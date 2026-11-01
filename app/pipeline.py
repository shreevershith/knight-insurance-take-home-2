"""Pipeline: pages -> classify -> extract -> resolve -> verify -> conflicts -> rules -> AI analyst.

Each step writes a progress line (shown in the UI). A failure on one page marks that page
(MODEL_ERROR / UNREADABLE) and the run continues; missing data then shows up as missing, never guessed.
"""
import traceback
from datetime import datetime, timezone
from collections import defaultdict

from app import config, conflicts, llm
from app.classify import classify_page
from app.db import Conflict, Document, Field, JobLog, Page, RuleResult, SessionLocal, Submission, audit
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
    the caller that flips the row to PROCESSING runs the job; a second click or worker gets False.
    Production: the job table is claimed with SELECT ... FOR UPDATE SKIP LOCKED and a lease (D-25)."""
    with SessionLocal() as db:
        n = (db.query(Submission).filter(Submission.id == submission_id, Submission.status != "PROCESSING")
             .update({"status": "PROCESSING", "step": "Queued"}, synchronize_session=False))
        db.commit()
        return n == 1


def reset_interrupted() -> int:
    """At startup nothing is running yet, so a submission still PROCESSING was cut off by a restart.
    It is marked INTERRUPTED (visible, with a log line) rather than left spinning forever.
    Production: a worker lease that expires does this without a restart."""
    with SessionLocal() as db:
        stuck = db.query(Submission).filter(Submission.status == "PROCESSING").all()
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
    llm.run_id = f"sub{submission_id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    if config.RUN_LABEL:
        llm.run_id += f"-{config.RUN_LABEL}"
    try:
        reset_for_reprocess(db, sub)
        progress(db, sub, "Splitting files into pages")
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
        progress(db, sub, f"Classifying {len(pages)} pages by content")
        for p in pages:
            _classify(db, sub, p)
        _inherit_continuations(sub)
        db.commit()

        progress(db, sub, "Extracting fields with evidence")
        known_drivers: list[dict] = []
        for d in sub.documents:
            if d.status != "OK":
                continue
            page_ctx = {"doc_id": d.id}
            for p in d.pages:
                _extract(db, sub, d, p, page_ctx, known_drivers)
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

        from app.analyst import analyze
        progress(db, sub, "AI analyst: summary, concerns, recommendations")
        try:
            sub.summary = analyze(db, sub)
        except llm.LLMError as exc:
            sub.summary = {"error": f"MODEL_ERROR: {exc}"}
            progress(db, sub, f"AI analyst failed: {exc}", "ERROR")
        sub.status = "DONE"
        progress(db, sub, f"Done: {sub.decision} -> {sub.team}")
    except Exception as exc:
        db.rollback()
        sub = db.get(Submission, submission_id)
        sub.status = "FAILED"
        progress(db, sub, f"Pipeline failed: {type(exc).__name__}: {exc}", "ERROR")
        log.error("pipeline failed", submission_id=submission_id, trace=traceback.format_exc())
    finally:
        db.close()


def run_analysis(submission_id: int, claimed: bool = False) -> None:
    """Re-run only the AI analyst (after human review made its findings out of date)."""
    if not claimed and not claim(submission_id):
        return
    from app.analyst import analyze
    db = SessionLocal()
    sub = db.get(Submission, submission_id)
    llm.run_id = f"sub{submission_id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-analysis"
    try:
        progress(db, sub, "AI analyst: re-running on the reviewed values")
        try:
            sub.summary = analyze(db, sub)
        except llm.LLMError as exc:
            sub.summary = {"error": f"MODEL_ERROR: {exc}"}
            progress(db, sub, f"AI analyst failed: {exc}", "ERROR")
        sub.status = "DONE"
        progress(db, sub, f"Done: {sub.decision} -> {sub.team}")
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


def _classify(db, sub, page: Page):
    ctx = {"submission_id": sub.id, "page_id": page.id}
    try:
        classify_page(page, ctx)
        if page.status == "PENDING":
            page.status = "OK"
    except llm.LLMError as exc:
        page.status, page.error = "MODEL_ERROR", str(exc)
        progress(db, sub, f"{page.document.filename} p{page.page_no}: MODEL_ERROR ({exc})", "ERROR")
        return
    if page.status == "UNREADABLE":
        progress(db, sub, f"{page.document.filename} p{page.page_no}: UNREADABLE ({page.error})", "WARN")
    elif page.doc_type in ("OTHER", "") or page.classify_confidence < 0.5:
        page.status = "UNCLASSIFIED"
    progress(db, sub, f"{page.document.filename} p{page.page_no}{' [' + page.label + ']' if page.label else ''}"
                      f" -> {page.doc_type} ({page.classify_method}, {page.classify_confidence:.2f})")


def _inherit_continuations(sub):
    """A continuation page is extracted as the document type it continues (an IFTA table's second
    page holds rows and the total). Pages the content rules marked as a report trailer ("END OF
    REPORT FOR ...") stay CONTINUATION: there is nothing on them to extract."""
    for d in sub.documents:
        prev = None
        for p in d.pages:
            if p.doc_type == "CONTINUATION" and prev:
                p.classify_reason += f" (continues {prev})"
                if p.classify_method != "rules" and prev in SPEC:
                    p.doc_type = prev
            elif p.doc_type:
                prev = p.doc_type


def _extract(db, sub, doc: Document, page: Page, page_ctx: dict, known_drivers: list[dict]):
    if page.status != "OK" or page.doc_type not in SPEC:
        if page.status == "OK":
            page.status = "SKIPPED"
        return
    ctx = {"submission_id": sub.id, "page_id": page.id, "doc_type": page.doc_type}
    try:
        if page.text_source == "xlsx" and page.doc_type in TABULAR_TYPES:
            facts, method = extract_sheet(config.data_path(doc.blob_path), page.page_no - 1), "xlsx"
            if not facts:
                facts, method = extract_page(page, page.doc_type, False, ctx), "llm_text"
        else:
            # The model produced these values (as transcript or extraction), so they get a model prior
            # even when the page is a spreadsheet (D-21). "xlsx" means deterministic only.
            method = "llm_vision" if page.text_source == "vision_transcript" else "llm_text"
            facts = None
            if page.doc_type == "IFTA_RETURN" and page.text_source == "vision_transcript":
                facts = parse_ifta_page(page.text, page_ctx)  # D-31: table rows read by code
                if facts:
                    progress(db, sub, f"{doc.filename} p{page.page_no}: IFTA table parsed in code")
            if not facts:
                facts = _llm_extract(db, sub, doc, page, ctx)
    except llm.LLMError as exc:
        page.status, page.error = "MODEL_ERROR", str(exc)
        progress(db, sub, f"{doc.filename} p{page.page_no}: extraction MODEL_ERROR ({exc})", "ERROR")
        return

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


def _llm_extract(db, sub, doc, page, ctx):
    """Extraction on the task's tier; one re-run on the escalation model if the result fails the code
    checks (model error, or too many values whose excerpt isn't on the page)."""
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
        if not cheap or not facts:
            return facts
        if missing:
            reason = f"key fields not extracted: {', '.join(missing)}"
        elif unverified / len(facts) > ESCALATE_IF_UNVERIFIED:
            reason = f"{unverified}/{len(facts)} excerpts not found on the page"
        else:
            return facts
    progress(db, sub, f"{doc.filename} p{page.page_no}: escalated to {config.ESCALATION_MODEL} ({reason})", "WARN")
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
        page_ctx = {"doc_id": doc.id}
        for page in doc.pages:
            if page.text_source == "vision_transcript":
                try:
                    classify_page(page, {"submission_id": sub.id, "page_id": page.id}, escalate=True, reason=reason)
                except llm.LLMError as exc:
                    page.status, page.error = "MODEL_ERROR", str(exc)
                    continue
            _extract(db, sub, doc, page, page_ctx, known_drivers)
    db.flush()
    _cross_document_confirmation(db, sub.id)
