"""Pipeline: pages -> classify -> extract -> resolve -> verify -> conflicts -> rules -> AI analyst.

Each step writes a progress line (shown in the UI). A failure on one page marks that page
(MODEL_ERROR / UNREADABLE) and the run continues; missing data then shows up as missing, never guessed.
"""
import contextvars
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

from app import commodities, config, conflicts, llm
from app.classify import classify_page
from app.db import Conflict, Document, Field, Job, JobLog, Page, RuleResult, SessionLocal, Submission, audit
from app.extract import evidence
from app.extract.llm_extract import extract_page
from app.extract.normalize import norm_id, normalize, parse_number, validate
from app.extract.spec import SPEC
from app.extract.table_transcript import parse_ifta_page
from app.extract.xlsx_extract import extract_sheet
from app.intake import split_pages
from app.log import get_logger
from app.resolve import assign_keys

log = get_logger(__name__)
TABULAR_TYPES = {"DRIVER_SCHEDULE", "VEHICLE_SCHEDULE"}
FORM_TYPES = {"QUOTE_FORM", "APPLICATION"}  # forms: send the page image too (checkbox states)
ESCALATE_IF_UNVERIFIED = 0.2  # share of a page's values whose excerpt isn't in the page -> re-run on Opus


def progress(db, sub: Submission, message: str, level: str = "INFO") -> None:
    sub.step = message[:100]
    db.add(JobLog(submission_id=sub.id, level=level, message=message))
    db.commit()
    log.info(message, submission_id=sub.id)


def claim(submission_id: int) -> bool:
    """One active job per submission. An atomic conditional UPDATE (safe on SQLite and Postgres): only
    the caller that flips the row to PROCESSING runs (or enqueues) the job; a second click gets False.
    In queue mode the job itself is then claimed by a worker with SKIP LOCKED and a lease (app/jobs.py)."""
    with SessionLocal() as db:
        n = (db.query(Submission).filter(Submission.id == submission_id, Submission.status != "PROCESSING")
             .update({"status": "PROCESSING", "step": "Queued"}, synchronize_session=False))
        db.commit()
        return n == 1


def reset_interrupted() -> int:
    """At web startup no in-process job is running yet, so a submission still PROCESSING without a queued
    job was cut off by a restart. It is marked INTERRUPTED (visible, with a log line) rather than left
    spinning forever. Submissions with a queue job (D-36) are left alone: the workers own those, and an
    expired lease hands a cut-off job to another worker."""
    with SessionLocal() as db:
        queued = db.query(Job.submission_id).filter(Job.status.in_(("QUEUED", "RUNNING")))
        stuck = db.query(Submission).filter(Submission.status == "PROCESSING", Submission.id.notin_(queued)).all()
        for sub in stuck:
            sub.status = "INTERRUPTED"
            sub.step = "Interrupted by a restart: reprocess to continue"
            db.add(JobLog(submission_id=sub.id, level="WARN", message=sub.step))
        db.commit()
        return len(stuck)


def run(submission_id: int, claimed: bool = False) -> None:
    if not claimed and not claim(submission_id):
        log.warning("submission already processing; not started twice", submission_id=submission_id)
        return
    db = SessionLocal()
    sub = db.get(Submission, submission_id)
    label = f"-{config.RUN_LABEL}" if config.RUN_LABEL else ""
    llm.run_id.set(f"sub{submission_id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}{label}")
    try:
        reset_for_reprocess(db, sub)
        progress(db, sub, "Splitting files into pages")
        for d in sub.documents:
            if d.status == "UNSUPPORTED" and d.error.startswith("unsupported file type") and not d.pages:
                d.status, d.error = "OK", ""  # a format added since the last run (e.g. .docx) is tried again
        docs = [d for d in sub.documents if d.status == "OK"]
        for d in docs:
            if not d.pages:
                split_pages(db, d)
        db.commit()
        db.expire_all()  # reload relationships so the new Page rows are visible
        for d in sub.documents:
            if d.status in ("DUPLICATE", "UNREADABLE", "UNSUPPORTED"):
                progress(db, sub, f"{d.filename}: {d.status} {d.error or ''}".strip(), "WARN")

        pages = [p for d in sub.documents if d.status == "OK" for p in d.pages]
        progress(db, sub, f"Classifying {len(pages)} pages by content ({config.LLM_CONCURRENCY} at a time)")
        _classify_all(db, sub, pages)
        _inherit_continuations(sub)
        db.commit()

        progress(db, sub, "Extracting fields with evidence")
        # The model calls run in parallel; keys, scoring and storage then run in page order, because
        # entity keys carry over between pages of a document (a driver's MVR, an IFTA return's quarter).
        prepared = _prepare_facts(sub, [d for d in sub.documents if d.status == "OK"])
        known_drivers: list[dict] = []
        for d in sub.documents:
            if d.status != "OK":
                continue
            page_ctx = {"doc_id": d.id}
            for p in d.pages:
                _extract(db, sub, d, p, page_ctx, known_drivers, prepared.get(p.id))
        db.commit()

        # before human decisions are re-applied, so a corrected category wins over the new one (D-49)
        progress(db, sub, "Sorting commodities against the prohibited cargo list")
        try:
            commodities.classify(db, sub)
        except llm.LLMError as exc:
            progress(db, sub, f"Commodity sorting failed ({exc}); the rules use keywords only", "WARN")
        db.commit()

        reapplied = reapply_human_decisions(db, sub.id)
        if reapplied:
            progress(db, sub, f"Re-applied human decisions over {reapplied} re-extracted values")
        progress(db, sub, "Cross-checking values across documents")
        _cross_document_confirmation(db, sub.id)
        _loss_runs_confirmed_by_listing(db, sub.id)
        failed = _ifta_reconciliation(db, sub.id)
        if failed and config.route("transcribe")[0] != config.ESCALATION_MODEL:
            _escalate_ifta(db, sub, failed, known_drivers)
            failed = _ifta_reconciliation(db, sub.id)
        if failed:
            progress(db, sub, f"IFTA return(s) {', '.join(failed)} not confirmed: rows don't match a printed "
                              "total (or none was found). Their values stay LOW_CONFIDENCE.", "WARN")
        db.commit()

        progress(db, sub, "Detecting conflicts and duplicates")
        found = conflicts.detect(db, sub.id)
        db.commit()
        progress(db, sub, f"{len(found)} conflicts/duplicates found")

        from app.rules.engine import evaluate  # local import: engine imports pipeline helpers
        progress(db, sub, "Applying guideline rules")
        evaluate(db, sub, actor="system")
        db.commit()

        progress(db, sub, "AI analyst: summary, concerns, recommendations")
        _analyze_and_finish(db, sub)
    except Exception as exc:
        db.rollback()
        sub = db.get(Submission, submission_id)
        sub.status = "FAILED"
        progress(db, sub, f"Pipeline failed: {type(exc).__name__}: {exc}", "ERROR")
        log.error("pipeline failed", submission_id=submission_id, trace=traceback.format_exc())
    finally:
        db.close()


def _analyze_and_finish(db, sub: Submission) -> None:
    """The AI analysis (a model failure is recorded, not raised: the rules decision stands), then DONE."""
    from app.analyst import analyze  # imported on use, as before
    try:
        sub.summary = analyze(db, sub)
    except llm.LLMError as exc:
        sub.summary = {"error": f"MODEL_ERROR: {exc}"}
        progress(db, sub, f"AI analyst failed: {exc}", "ERROR")
    sub.status = "DONE"
    progress(db, sub, f"Done: {sub.decision} -> {sub.team}")


def run_analysis(submission_id: int, claimed: bool = False) -> None:
    """Re-run only the AI analyst (after human review made its findings out of date)."""
    if not claimed and not claim(submission_id):
        return
    db = SessionLocal()
    sub = db.get(Submission, submission_id)
    llm.run_id.set(f"sub{submission_id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-analysis")
    try:
        progress(db, sub, "AI analyst: re-running on the reviewed values")
        _analyze_and_finish(db, sub)
    except Exception as exc:
        db.rollback()
        sub = db.get(Submission, submission_id)
        sub.status = "FAILED"
        progress(db, sub, f"Analysis failed: {type(exc).__name__}: {exc}", "ERROR")
    finally:
        db.close()


def reset_for_reprocess(db, sub):
    """Start a new extraction run without destroying the previous one.

    Nothing is deleted: previous extracted fields and conflicts are marked REPLACED (kept for audit,
    hidden from rules), rule-run history stays, and human decisions (method="human" fields) stay
    active. After re-extraction they are re-applied by (entity_key, field), not by row id."""
    for f in db.query(Field).filter(Field.submission_id == sub.id, Field.method != "human",
                                    Field.status != "REPLACED"):
        f.validation = {**(f.validation or {}), "status_before_replace": f.status}
        f.status = "REPLACED"
    for c in db.query(Conflict).filter(Conflict.submission_id == sub.id, Conflict.status != "REPLACED"):
        c.description = f"[{c.status} before reprocess] {c.description}"
        c.status = "REPLACED"
    sub.decision, sub.team, sub.summary = "", "", {}
    for d in sub.documents:
        for p in d.pages:
            p.status, p.error = "PENDING", ""
    db.add(JobLog(submission_id=sub.id, level="INFO", message="=== New run: previous values kept as REPLACED ==="))
    db.commit()


def reapply_human_decisions(db, submission_id) -> int:
    """Human decisions win over freshly extracted values of the same fact."""
    humans = db.query(Field).filter(Field.submission_id == submission_id, Field.method == "human",
                                    Field.status == "OK").all()
    decided = {(h.entity_key, h.name): h for h in humans}
    count = 0
    for f in db.query(Field).filter(Field.submission_id == submission_id, Field.method != "human",
                                    Field.status.in_(("OK", "LOW_CONFIDENCE"))):
        h = decided.get((f.entity_key, f.name))
        if h:
            f.status = "SUPERSEDED"
            f.validation = {**(f.validation or {}), "superseded_by_human": h.id}
            count += 1
    return count


# ---------------------------------------------------------------- parallel page calls (D-36)

_PAGE_IN = ("id", "page_no", "label", "text", "text_source", "has_text_layer", "image_path", "doc_type",
            "classify_method", "classify_confidence", "classify_reason", "status", "error")
_CLASSIFIED = ("doc_type", "classify_confidence", "classify_reason", "classify_method", "status", "error",
               "text", "text_source")


def _snapshot(page: Page) -> SimpleNamespace:
    """A plain copy of a page for a worker thread: threads never touch the ORM session or its objects."""
    return SimpleNamespace(**{k: getattr(page, k) for k in _PAGE_IN})


def _parallel(fn, items: list) -> list:
    """fn(item) for each item, up to config.LLM_CONCURRENCY at a time, results in input order. Each call
    runs in a copy of the caller's context, so the cost ledger keeps this job's run id. An LLMError is
    returned as the result (a failed page is marked, the run goes on); any other error is raised."""
    def call(item):
        try:
            return fn(item)
        except llm.LLMError as exc:
            return exc
    if config.LLM_CONCURRENCY <= 1 or len(items) <= 1:
        return [call(i) for i in items]
    with ThreadPoolExecutor(max_workers=config.LLM_CONCURRENCY) as pool:
        futures = [pool.submit(contextvars.copy_context().run, call, i) for i in items]
        return [f.result() for f in futures]


def _classify_all(db, sub, pages: list[Page]) -> None:
    sub_id = sub.id

    def work(snap):
        classify_page(snap, {"submission_id": sub_id, "page_id": snap.id})
        return snap
    for page, result in zip(pages, _parallel(work, [_snapshot(p) for p in pages])):
        _classify(db, sub, page, result)


def _classify(db, sub, page: Page, result):
    """Apply one page's classification: a classified snapshot, or the LLMError it raised."""
    if isinstance(result, llm.LLMError):
        page.status, page.error = "MODEL_ERROR", str(result)
        progress(db, sub, f"{page.document.filename} p{page.page_no}: MODEL_ERROR ({result})", "ERROR")
        return
    for k in _CLASSIFIED:
        setattr(page, k, getattr(result, k))
    if page.status == "PENDING":
        page.status = "OK"
    if page.status == "UNREADABLE":
        progress(db, sub, f"{page.document.filename} p{page.page_no}: UNREADABLE ({page.error})", "WARN")
    elif not page.doc_type or page.classify_confidence < 0.5:
        # type unknown: the page might be an application or loss run we failed to read, so rules treat it as unread
        page.status = "UNCLASSIFIED"
    # A page confidently classified OTHER (an agent's cover note) was read: it holds nothing the guideline
    # asks for. It stays OK and extraction skips it, so it can't turn "missing" into "could not be read".
    progress(db, sub, f"{page.document.filename} p{page.page_no}{' [' + page.label + ']' if page.label else ''}"
                      f" -> {page.doc_type} ({page.classify_method}, {page.classify_confidence:.2f})")


def _inherit_continuations(sub):
    """A continuation page is extracted as the document type it continues (an IFTA table's second
    page holds rows and the total). Pages the content rules marked as a report trailer ("END OF
    REPORT FOR ...") stay CONTINUATION: there is nothing on them to extract."""
    for d in sub.documents:
        _inherit_in_document(d)


def _inherit_in_document(doc):
    prev = None
    for p in doc.pages:
        if p.doc_type == "CONTINUATION" and prev:
            p.classify_reason += f" (continues {prev})"
            if p.classify_method != "rules" and prev in SPEC:
                p.doc_type = prev
        elif p.doc_type:
            prev = p.doc_type


def _extractable(page: Page) -> bool:
    return page.status == "OK" and page.doc_type in SPEC


def _in_page_order(page) -> bool:
    """A scanned IFTA page is parsed in code with the header remembered from the page before it, so it
    runs in page order (no model call unless the parse fails)."""
    return page.doc_type == "IFTA_RETURN" and page.text_source == "vision_transcript"


def _prepare_facts(sub, docs) -> dict:
    """Page id -> _page_facts result (or the LLMError) for every extractable page, model calls in parallel."""
    sub_id = sub.id
    todo = [(p.id, _snapshot(p), d.blob_path) for d in docs for p in d.pages
            if _extractable(p) and not _in_page_order(p)]

    def work(item):
        page_id, snap, blob = item
        return _page_facts(snap, blob, {"submission_id": sub_id, "page_id": page_id, "doc_type": snap.doc_type})
    return {page_id: result for (page_id, _, _), result in zip(todo, _parallel(work, todo))}


def _page_facts(page, blob_path: str, ctx: dict, page_ctx: dict | None = None):
    """One page's values: (facts, method, notes). No database access, so it is safe on a worker thread;
    notes are progress lines for the caller to log. Raises llm.LLMError."""
    notes: list[tuple[str, str]] = []
    if page.text_source == "xlsx" and page.doc_type in TABULAR_TYPES:
        facts = extract_sheet(config.data_path(blob_path), page.page_no - 1)
        if facts:
            return facts, "xlsx", notes
        return extract_page(page, page.doc_type, False, ctx), "llm_text", notes
    # The model produced these values (as transcript or extraction), so they get a model prior
    # even when the page is a spreadsheet (D-21). "xlsx" means deterministic only.
    method = "llm_vision" if page.text_source == "vision_transcript" else "llm_text"
    if page_ctx is not None and _in_page_order(page):
        facts = parse_ifta_page(page.text, page_ctx)  # D-31: table rows read by code
        if facts:
            notes.append(("IFTA table parsed in code", "INFO"))
            return facts, method, notes
    return _llm_extract(page, ctx, notes), method, notes


def _extract(db, sub, doc: Document, page: Page, page_ctx: dict, known_drivers: list[dict], prepared=None):
    """Store one page's values. prepared: its _page_facts result computed ahead (in parallel), or None to
    compute it here, in page order."""
    if not _extractable(page):
        if page.status == "OK":
            page.status = "SKIPPED"
        return
    if prepared is None:
        ctx = {"submission_id": sub.id, "page_id": page.id, "doc_type": page.doc_type}
        try:
            prepared = _page_facts(_snapshot(page), doc.blob_path, ctx, page_ctx)
        except llm.LLMError as exc:
            prepared = exc
    if isinstance(prepared, llm.LLMError):
        page.status, page.error = "MODEL_ERROR", str(prepared)
        progress(db, sub, f"{doc.filename} p{page.page_no}: extraction MODEL_ERROR ({prepared})", "ERROR")
        return
    facts, method, notes = prepared
    for message, level in notes:
        progress(db, sub, f"{doc.filename} p{page.page_no}: {message}", level)

    assign_keys(facts, page_ctx, known_drivers)
    low = 0
    for f in facts:
        if f["entity_type"] == "sheet_note":
            method_used = "xlsx"
        else:
            method_used = method
        checks = validate(f["field"], f["value"])
        conf, ev = evidence.score(method_used, f["field"], f["value"], f["excerpt"], page.text, checks)
        status = "OK" if conf >= evidence.REVIEW_THRESHOLD else "LOW_CONFIDENCE"
        low += status != "OK"
        db.add(Field(submission_id=sub.id, page_id=page.id, doc_type=page.doc_type,
                     entity_type=f["entity_type"], entity_key=f["entity_key"] or "", name=f["field"],
                     value=f["value"], excerpt=f["excerpt"], method=method_used, confidence=conf,
                     evidence_verified=bool(ev.get("excerpt_in_source")), validation={**ev, **checks},
                     status=status))
    progress(db, sub, f"{doc.filename} p{page.page_no}: {len(facts)} values ({low} low confidence)")


# Fields an entity is keyed by: a cheap-tier page without them is re-run on the escalation model
# (Haiku once dropped a loss run's line of business, so the AL policy was keyed "loss_run:?:...").
KEY_FIELDS = {"LOSS_RUN": ("line", "policy_number"), "DRIVER_LICENSE": ("license_number",)}


def _llm_extract(page, ctx, notes: list):
    """Extraction on the task's tier; one re-run on the escalation model if the result fails the code
    checks (model error, no values, missing key fields, or too many values whose excerpt isn't on the
    page). The escalation goes into `notes` for the caller to log."""
    use_image = (page.text_source == "vision_transcript" or page.doc_type in FORM_TYPES) and bool(page.image_path)
    cheap = config.route("extract")[0] != config.ESCALATION_MODEL
    try:
        facts = extract_page(page, page.doc_type, use_image, ctx)
    except llm.LLMError as exc:
        if not cheap:
            raise
        reason = f"model error: {exc}"
    else:
        unverified = sum(not evidence.excerpt_in_source(f["excerpt"], page.text) for f in facts)
        missing = [k for k in KEY_FIELDS.get(page.doc_type, ()) if not any(f["field"] == k for f in facts)]
        if not cheap:
            return facts
        if not facts:
            reason = "no values extracted"  # an empty page would otherwise read as 'nothing there'
        elif missing:
            reason = f"key fields not extracted: {', '.join(missing)}"
        elif unverified / len(facts) > ESCALATE_IF_UNVERIFIED:
            reason = f"{unverified}/{len(facts)} excerpts not found on the page"
        else:
            return facts
    notes.append((f"escalated to {config.ESCALATION_MODEL} ({reason})", "WARN"))
    return extract_page(page, page.doc_type, use_image, ctx, escalate=True, reason=reason)


def _cross_document_confirmation(db, submission_id):
    """A value that two independent documents agree on is confirmed (confidence raised)."""
    fields = conflicts.active_fields(db, submission_id)
    groups = defaultdict(list)
    for f in fields:
        if f.method == "human":
            continue
        n = normalize(f.name, f.value)
        if n is not None:
            groups[(f.entity_key, f.name, str(n))].append(f)
    for items in groups.values():
        docs = {f.page.document_id for f in items if f.page}
        if len(docs) >= 2:
            for f in items:
                if f.confidence < 0.95:
                    f.validation = {**f.validation, "confirmed_by_other_document": True}
                    f.confidence = max(f.confidence, 0.9)
                    f.status = "OK" if f.confidence >= evidence.REVIEW_THRESHOLD else f.status


def _loss_runs_confirmed_by_listing(db, submission_id):
    """A loss run whose policy number, start and end match a prior term listed on the application or
    quote is confirmed by that listing: two independent documents agree. Matters for photographed or
    scanned loss runs, whose values start below the review threshold (held-out finding, HO1)."""
    fields = conflicts.active_fields(db, submission_id)
    listed = defaultdict(dict)  # policy number -> {field: normalized value}
    for f in fields:
        if f.entity_key.startswith("prior_policy:") and f.method != "human":
            listed[f.entity_key][f.name] = normalize(f.name, f.value)
    by_policy = {terms.get("policy_number"): terms for terms in listed.values() if terms.get("policy_number")}
    for f in fields:
        if not f.entity_key.startswith("loss_run:") or f.method == "human" or f.confidence >= evidence.REVIEW_THRESHOLD:
            continue
        policy = norm_id(f.entity_key.rsplit(":", 1)[-1])
        terms = by_policy.get(policy)
        if terms and f.name in ("policy_number", "term_start", "term_end") and terms.get(f.name) == normalize(f.name, f.value):
            f.validation = {**f.validation, "confirmed_by_prior_insurance_listing": True}
            f.confidence = max(f.confidence, 0.9)
            f.status = "OK"


def _ifta_reconciliation(db, submission_id):
    """Scanned IFTA tables: jurisdiction rows must add up to the printed total. If they do, the
    transcription is very likely right, so the return's values are confirmed."""
    fields = conflicts.active_fields(db, submission_id)
    rows = defaultdict(list)
    totals = {}
    failed = []
    for f in fields:
        if f.entity_type == "ifta_row" and f.name == "total_miles":
            rows[f.entity_key.split(":")[1]].append(f)
        if f.entity_type == "ifta" and f.name == "total_miles":
            totals[f.entity_key.split(":", 1)[1]] = f
    periods = {f.entity_key.split(":")[1] for f in fields if f.entity_type in ("ifta", "ifta_row")}
    for period in sorted(periods):
        total_field = totals.get(period)
        row_sum = sum(parse_number(f.value) or 0 for f in rows.get(period, []))
        # No printed total, or no rows, is a failed check too: the return can't be confirmed.
        printed = parse_number(total_field.value) if total_field else None
        ok = printed is not None and bool(rows.get(period)) and abs(row_sum - printed) < 0.5
        related = [f for f in fields if f.entity_key.startswith(f"ifta:{period}") or f.entity_key.startswith(f"ifta_row:{period}")]
        for f in related:
            f.validation = {**f.validation, "ifta_rows_sum_to_total": ok}
            if ok and f.method == "llm_vision":
                f.confidence = max(f.confidence, 0.9)
                f.status = "OK"
        log.info("ifta reconciliation", period=period, row_sum=row_sum, printed=printed, ok=ok)
        if not ok:
            failed.append(period)
    return failed


def _escalate_ifta(db, sub, periods, known_drivers):
    """An IFTA return whose rows don't add up to its printed total was likely mis-transcribed: re-read
    that document's scanned pages on the escalation model, replace its values, and re-check."""
    fields = conflicts.active_fields(db, sub.id)
    doc_ids = {f.page.document_id for f in fields if f.page and f.method == "llm_vision" and any(
        f.entity_key in (f"ifta:{p}",) or f.entity_key.startswith(f"ifta_row:{p}:") for p in periods)}
    for doc in (db.get(Document, i) for i in doc_ids):
        reason = f"IFTA rows do not reconcile to the printed total ({', '.join(periods)})"
        progress(db, sub, f"{doc.filename}: re-transcribing on {config.ESCALATION_MODEL} ({reason})", "WARN")
        for f in db.query(Field).filter(Field.page_id.in_([pg.id for pg in doc.pages]), Field.method != "human",
                                        Field.status != "REPLACED"):
            f.status = "REPLACED"
        # Re-transcribe every scanned page first, then re-apply continuation inheritance for the whole
        # document: a re-classified table page that comes back CONTINUATION is still read as the return.
        for page in doc.pages:
            if page.text_source != "vision_transcript" and page.status != "MODEL_ERROR":
                continue
            page.status, page.error = "OK", ""  # a page that failed the first pass gets its retry too
            try:
                classify_page(page, {"submission_id": sub.id, "page_id": page.id}, escalate=True, reason=reason)
            except llm.LLMError as exc:
                page.status, page.error = "MODEL_ERROR", str(exc)
        _inherit_in_document(doc)
        page_ctx = {"doc_id": doc.id}
        for page in doc.pages:
            if page.status == "SKIPPED":
                page.status = "OK"  # _extract decides again from the new type
            _extract(db, sub, doc, page, page_ctx, known_drivers)
    db.flush()
    reapply_human_decisions(db, sub.id)  # human decisions win over the re-read values too
    _cross_document_confirmation(db, sub.id)
