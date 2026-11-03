"""Underwriter web app."""
import json
import re
from datetime import timezone
import threading
from collections import defaultdict
from pathlib import Path

from sqlalchemy import func
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from urllib.parse import quote, urlparse

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import config, graph, jobs, pipeline, review, tracing
from app.config import data_path
from app.web.labels import (action_label, capitalize_first, check_lines, doc_type_label, entity_labels, field_label,
                            based_on, label_for, problems_only, readable_excerpt, rule_lines,
                            method_label, plain_error, status_label)
from app.db import (latest_rule_results, AuditEvent, Conflict, Document, Field, Job, JobLog, Page, RuleResult, SessionLocal, Submission,
                    audit, init_db, utcnow)
from app.analyst import agent_email
from app.intake import ingest_eml, ingest_files
from app.log import setup_logging

setup_logging()
init_db()
pipeline.reset_interrupted()  # nothing runs before startup: anything still PROCESSING was cut off
app = FastAPI(title="Knight Commercial Auto Intake")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
REVIEWER = "underwriter@knight"  # prototype: no SSO; production uses the signed-in user (RBAC)


def _start(submission_id: int, kind: str = "run") -> None:
    """One active job per submission (409 otherwise). JOB_MODE=queue: a worker process runs it (D-36);
    thread: this process does, in the background."""
    if not pipeline.claim(submission_id):
        raise HTTPException(409, "this submission is already being processed")
    if config.JOB_MODE == "queue":
        jobs.enqueue(submission_id, kind)
    else:
        threading.Thread(target=jobs.KINDS[kind], args=(submission_id, True), daemon=True).start()


# Pipeline stages shown as a stepper; each starts with one of these progress messages.
STAGES = [("Splitting", "Split"), ("Classifying", "Classify"), ("Extracting", "Extract"),
          ("Cross-checking", "Cross-check"), ("Detecting", "Conflicts"), ("Applying", "Rules"),
          ("AI analyst", "AI analyst"), ("Done", "Done")]


def _stage(logs) -> int:
    """Index of the current stage, from the progress log of the latest run."""
    current = -1
    for entry in logs:
        if entry.message.startswith("=== New run"):
            current = -1
        for i, (prefix, _) in enumerate(STAGES):
            if entry.message.startswith(prefix):
                current = i
    return current


# Extracted values grouped by kind for the page (entity-key prefix -> group).
VALUE_GROUPS = [("Applicant, coverage & operations", ("applicant", "coverage", "operations", "commodity")),
                ("Drivers", ("driver",)), ("MVR history", ("violation", "license_event")), ("Vehicles", ("vehicle",)),
                ("Loss history & prior insurance", ("loss_run", "claim", "prior_policy")),
                ("IFTA fuel-tax returns", ("ifta",)), ("Notes from spreadsheets", ("sheet_note",))]
IFTA_COLUMNS = ["jurisdiction", "surcharge", "total_miles", "taxable_miles", "tax_paid_gallons", "total"]


def _group_values(entities: dict) -> tuple[list, dict]:
    """[(label, [(entity_key, fields)])] in a fixed order, plus IFTA jurisdiction rows per quarter as
    table rows ({column: field}), so ~600 row values show as four compact tables."""
    groups = {label: [] for label, _ in VALUE_GROUPS}
    groups["Other"] = []
    ifta_rows: dict[str, list] = defaultdict(list)
    for key, items in entities.items():
        kind = key.split(":", 1)[0]
        if kind == "ifta_row":
            period = key.split(":")[1]
            ifta_rows[period].append({"key": key, "cells": {f.name: f for f in items},
                                      "low": any(f.status == "LOW_CONFIDENCE" for f in items)})
            continue
        label = next((lab for lab, kinds in VALUE_GROUPS if kind in kinds), "Other")
        groups[label].append((key, items, _facts(items)))
    return [(label, rows) for label, rows in groups.items() if rows], dict(ifta_rows)


_PERSON_NAMES = {"full_name", "driver_name", "contact_name", "agency_contact"}


def _same_value_key(name: str, value) -> str:
    """When two readings are the same value: compared the way conflicts are (dates and case normalized,
    "RAMOS, HECTOR" = "HECTOR RAMOS"), so the page never shows as different what isn't a disagreement."""
    from app.extract.normalize import name_tokens, normalize
    if name in _PERSON_NAMES:
        return " ".join(sorted(name_tokens(str(value))))
    norm = normalize(name, value)
    return str(norm if norm not in (None, "") else " ".join(str(value).split()).rstrip(".").upper())


def _facts(items) -> list[dict]:
    """One row per fact instead of one per reading (D-57): the fact's distinct values, each with every document
    that stated it (so agreeing documents show as one value with several sources, disagreeing ones as separate
    values), and readings a person's decision replaced folded underneath. A fact with only replaced readings
    (e.g. a superseded older MVR) shows them as its values."""
    by_name: dict[str, list] = defaultdict(list)
    for f in items:
        by_name[f.name].append(f)
    facts = []
    for name, fields in by_name.items():
        active = [f for f in fields if f.status != "SUPERSEDED"]
        replaced = [f for f in fields if f.status == "SUPERSEDED"]
        if not active:
            active, replaced = replaced, []
        values: dict[str, list] = {}
        for f in sorted(active, key=lambda f: (f.method != "human", -f.confidence, f.id)):
            values.setdefault(_same_value_key(name, f.value), []).append(f)
        facts.append({"name": name, "values": list(values.values()), "replaced": replaced,
                      "low": any(f.status == "LOW_CONFIDENCE" for f in active),
                      "human": any(f.method == "human" for f in active)})
    return facts


def _summary_blocks(text: str) -> list[tuple[str, object]]:
    """AI summary -> [("lead", sentence), ("p", text), ("ul", [items])]. The model sometimes writes
    lists as '- item' lines or inline ': - a - b'; render those as real lists."""

    text = (text or "").strip()
    m = re.match(r"(.{40,}?[.!?])\s+(.*)", text, re.S)
    lead, rest = (m.group(1), m.group(2)) if m else (text, "")
    blocks: list = [("lead", lead)]
    for para in rest.splitlines():
        para = para.strip()
        if not para:
            continue
        if para.startswith("- "):
            if blocks[-1][0] == "ul":
                blocks[-1][1].append(para[2:])
            else:
                blocks.append(("ul", [para[2:]]))
        elif ": - " in para:
            head, items = para.split(": - ", 1)
            blocks.append(("p", head + ":"))
            blocks.append(("ul", [i.strip() for i in re.split(r"\s-\s", items) if i.strip()]))
        else:
            # a long paragraph reads better as short ones (three sentences each)
            sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", para)
            for i in range(0, len(sentences), 3):
                blocks.append(("p", " ".join(sentences[i:i + 3])))
    return blocks


templates.env.filters["summary_blocks"] = _summary_blocks
templates.env.globals["label_for"] = label_for
for _fn in (action_label, based_on, check_lines, doc_type_label, field_label, method_label, plain_error, problems_only,
            readable_excerpt, rule_lines, status_label):
    templates.env.globals[_fn.__name__] = _fn
templates.env.filters["capitalize_first"] = capitalize_first  # Jinja's capitalize would lowercase the rest
templates.env.globals["model_chip"] = lambda: " · ".join(
    f"{t} {config.route(t)[0].replace('claude-', '').split('-')[0].capitalize()}" for t in config.TIERS)


def _utc_iso(at) -> str:
    """ISO UTC timestamp for the browser. Postgres returns aware datetimes in the session's timezone;
    SQLite returns naive ones that were stored as UTC."""
    at = at.astimezone(timezone.utc).replace(tzinfo=None) if at.tzinfo else at
    return at.isoformat() + "Z"


def _review_error(exc: review.ReviewError) -> HTTPException:
    """409 while a job runs (try again later), 400 for a bad request."""
    return HTTPException(409 if "being processed" in str(exc) else 400, str(exc))


def _back(url: str, message: str, anchor: str = "", error: bool = False) -> RedirectResponse:
    """Redirect after an action, carrying a confirmation (or, with error, the reason it was refused) shown as a toast."""
    return RedirectResponse(f"{url}?{'err' if error else 'ok'}={quote(message)}{anchor}", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with SessionLocal() as db:
        subs = db.query(Submission).order_by(Submission.id.desc()).all()
        companies = {sid: value for sid, value in db.query(Field.submission_id, Field.value).filter(
            Field.entity_key == "applicant", Field.name == "business_name", Field.status == "OK")}
        open_counts = dict(db.query(Conflict.submission_id, func.count()).filter(Conflict.status == "OPEN")
                           .group_by(Conflict.submission_id))
        return templates.TemplateResponse(request, "index.html", {
            "subs": subs, "llm_ok": config.llm_configured(), "companies": companies, "open_counts": open_counts,
            "routes": {t: config.route(t)[0].replace("claude-", "") for t in config.TIERS}})


def _cost_runs(limit: int = 15) -> list[dict]:
    """Recent runs from the cost ledger: calls, cache hits, escalations, model time and cost per run."""
    runs: dict[str, dict] = {}
    try:
        lines = config.USAGE_LOG.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        run = runs.setdefault(r.get("run_id") or "(no run id)", {"run_id": r.get("run_id") or "(no run id)", "at": r["at"],
                                                                 "calls": 0, "cached": 0, "escalated": 0, "seconds": 0.0, "cost": 0.0})
        run["calls"] += 1
        run["cached"] += bool(r.get("cache_hit"))
        run["escalated"] += bool(r.get("escalated"))
        run["seconds"] += r.get("seconds") or 0.0
        run["cost"] += r.get("cost_usd") or 0.0
    return sorted(runs.values(), key=lambda r: r["at"], reverse=True)[:limit]


def _came_from(request: Request) -> dict:
    """Where 'back' goes from the operations page: the submission the person came from, else the list."""
    ref = urlparse(request.headers.get("referer", ""))
    m = re.fullmatch(r"/submissions/(\d+)", ref.path or "")
    if m and ref.netloc == request.url.netloc:
        return {"url": f"/submissions/{m.group(1)}", "label": f"Back to submission #{m.group(1)}"}
    return {"url": "/", "label": "All submissions"}


@app.get("/ops", response_class=HTMLResponse)
def ops(request: Request):
    """Operations view (read-only): the job queue, what needs attention, and cost per run."""
    with SessionLocal() as db:
        job_counts = dict(db.query(Job.status, func.count()).group_by(Job.status))
        recent_jobs = db.query(Job).order_by(Job.id.desc()).limit(20).all()
        subjects = {sid: subject for sid, subject in db.query(Submission.id, Submission.subject)}
        failed_subs = (db.query(Submission).filter(Submission.status.in_(("FAILED", "INTERRUPTED")))
                       .order_by(Submission.id.desc()).all())
        bad_pages = (db.query(Page).join(Document).filter(Page.status.in_(("MODEL_ERROR", "UNREADABLE", "UNCLASSIFIED")))
                     .order_by(Page.id.desc()).limit(40).all())
        bad_docs = (db.query(Document).filter(Document.status.in_(("UNREADABLE", "UNSUPPORTED")))
                    .order_by(Document.id.desc()).limit(40).all())
        attention = ([{"sub": p.document.submission_id, "file": f"{p.document.filename} p{p.page_no}", "status": p.status,
                       "why": p.error or p.classify_reason or ""} for p in bad_pages]
                     + [{"sub": d.submission_id, "file": d.filename, "status": d.status, "why": d.error or ""} for d in bad_docs])
        runs = _cost_runs()
        priced = [r for r in runs if r["calls"] > r["cached"]]
        return templates.TemplateResponse(request, "ops.html", {
            "job_counts": job_counts, "jobs": recent_jobs, "subjects": subjects, "failed_subs": failed_subs,
            "attention": sorted(attention, key=lambda a: -a["sub"]), "runs": runs,
            "escalation_rate": (sum(r["escalated"] for r in priced) / max(1, sum(r["calls"] - r["cached"] for r in priced))),
            "settings": {"Job mode": config.JOB_MODE, "Page calls at a time": config.LLM_CONCURRENCY,
                         "Lease": f"{config.JOB_LEASE_SECONDS}s", "Max attempts": config.JOB_MAX_ATTEMPTS},
            "now": _utc_iso(utcnow()), "back": _came_from(request)})


@app.post("/submissions")
async def create_submission(files: list[UploadFile], sender: str = Form(""), note: str = Form("")):
    """An agent's email, or files uploaded directly (simulated email intake). For files, the optional sender
    and note stand in for the email's From and body; a single .eml carries its own."""
    payload = [(f.filename or "file", await f.read()) for f in files if f.filename]
    if not payload:
        raise HTTPException(400, "no files")
    with SessionLocal() as db:
        if len(payload) == 1 and payload[0][0].lower().endswith(".eml"):
            sub = ingest_eml(db, payload[0][1])
        else:
            sub = ingest_files(db, payload, sender=sender.strip()[:255], note=note)
        db.commit()
        sub_id = sub.id
    _start(sub_id)
    return _back(f"/submissions/{sub_id}", "Submission received. Processing has started")


@app.post("/submissions/{sub_id}/reprocess")
def reprocess(sub_id: int):
    _start(sub_id)
    with SessionLocal() as db:
        audit(db, sub_id, REVIEWER, "submission.reprocess", f"submission:{sub_id}")
        db.commit()
    return _back(f"/submissions/{sub_id}", "Reprocessing started. Your review decisions will be re-applied")


@app.post("/submissions/{sub_id}/rerun-analysis")
def rerun_analysis(sub_id: int):
    _start(sub_id, "analysis")
    with SessionLocal() as db:
        audit(db, sub_id, REVIEWER, "analysis.rerun", f"submission:{sub_id}")
        db.commit()
    return _back(f"/submissions/{sub_id}", "Re-running the AI analysis", "#ai")


@app.get("/submissions/{sub_id}", response_class=HTMLResponse)
def show(request: Request, sub_id: int):
    with SessionLocal() as db:
        sub = db.get(Submission, sub_id)
        if not sub:
            raise HTTPException(404)
        docs = db.query(Document).filter(Document.submission_id == sub_id).order_by(Document.id).all()
        logs = db.query(JobLog).filter(JobLog.submission_id == sub_id).order_by(JobLog.id).all()
        # earlier runs' rows (REPLACED) are kept for audit but not shown here
        fields = (db.query(Field).filter(Field.submission_id == sub_id, Field.status != "REPLACED")
                  .order_by(Field.entity_key, Field.name).all())
        conflicts = (db.query(Conflict).filter(Conflict.submission_id == sub_id, Conflict.status != "REPLACED")
                     .order_by(Conflict.id).all())
        replaced = db.query(Field).filter(Field.submission_id == sub_id, Field.status == "REPLACED").count()
        rules = sorted(latest_rule_results(db, sub_id), key=lambda r: r.id)
        # what the previous rules run said, so the page shows what a re-run changed
        before = (db.query(RuleResult).filter(RuleResult.submission_id == sub_id, RuleResult.run_id != rules[0].run_id)
                  .order_by(RuleResult.id.desc()).first()) if rules else None
        previous = {r.rule_id: r.outcome for r in db.query(RuleResult).filter(
            RuleResult.submission_id == sub_id, RuleResult.run_id == before.run_id)} if before else {}
        audits = db.query(AuditEvent).filter(AuditEvent.submission_id == sub_id).order_by(AuditEvent.id.desc()).all()
        entities = defaultdict(list)
        for f in fields:
            entities[f.entity_key].append(f)
        field_by_id = {f.id: f for f in fields}
        # values that are one side of an open disagreement -> that conflict, so Extracted data can mark them
        # (claims and loss-run notes aren't a choice between values, so they aren't marked)
        disagree = {v["field_id"]: c.id for c in conflicts if c.status == "OPEN" and c.kind == "CONFLICT"
                    and c.field_name not in ("scheduled", "loss_run") for v in c.values}
        value_groups, ifta_rows = _group_values(dict(entities))
        names = entity_labels(fields)
        pages = {p.id: p for d in docs for p in d.pages}
        return templates.TemplateResponse(request, "submission.html", {
            "sub": sub, "docs": docs, "logs": logs, "entities": dict(entities), "conflicts": conflicts,
            "rules": rules, "audits": audits, "replaced": replaced, "field_by_id": field_by_id, "pages": pages,
            "disagree": disagree,
            "running": sub.status in ("RECEIVED", "PROCESSING"), "stages": STAGES, "stage": _stage(logs),
            "run_started": next((_utc_iso(entry.at) for entry in reversed(logs)
                                 if entry.message.startswith("=== New run")), ""),
            "low_confidence": sum(f.status == "LOW_CONFIDENCE" for f in fields),
            "value_groups": value_groups, "ifta_rows": ifta_rows, "names": names, "previous": previous,
            "ifta_columns": IFTA_COLUMNS,
            "company": next((f.value for f in sorted(fields, key=lambda f: f.method != "human")
                             if f.entity_key == "applicant" and f.name == "business_name"
                             and f.status in ("OK", "LOW_CONFIDENCE")), ""),
            "agent_email": agent_email(db, sub_id),
            "toast": request.query_params.get("ok", "")})


@app.get("/submissions/{sub_id}/status")
def status(sub_id: int, after: int = 0):
    """Polled by the page while a job runs: status, current stage, and new log lines."""
    with SessionLocal() as db:
        sub = db.get(Submission, sub_id)
        if not sub:
            raise HTTPException(404)
        # only the current run's lines: earlier runs' logs are kept, and this is polled every second
        run_start = db.query(func.max(JobLog.id)).filter(JobLog.submission_id == sub_id,
                                                         JobLog.message.like("=== New run%")).scalar() or 0
        logs = (db.query(JobLog).filter(JobLog.submission_id == sub_id, JobLog.id >= run_start)
                .order_by(JobLog.id).all())
        return JSONResponse({
            "status": sub.status, "step": sub.step, "decision": sub.decision, "team": sub.team,
            "stage": _stage(logs), "running": sub.status in ("RECEIVED", "PROCESSING"),
            "logs": [{"id": entry.id, "at": entry.at.strftime("%H:%M:%S"), "level": entry.level,
                      "message": entry.message} for entry in logs if entry.id > after]})


@app.get("/fields/{field_id}")
def field_detail(field_id: int):
    """Evidence for one value, loaded when the evidence drawer opens (keeps the page light)."""
    with SessionLocal() as db:
        f = db.get(Field, field_id)
        if not f:
            raise HTTPException(404)
        p = f.page
        return JSONResponse({
            "id": f.id, "entity": f.entity_key, "field": f.name, "value": f.value, "excerpt": f.excerpt or "",
            "method": f.method, "method_text": method_label(f.method), "label": field_label(f.name),
            "confidence": f.confidence, "status": f.status, "checks": f.validation or {},
            "check_lines": check_lines(f.validation),
            "excerpt_label": "Why it was put in this category" if f.method == "llm_category" else "Evidence on the page",
            "source": f"{p.document.filename} · page {p.page_no}" if p else "",
            "image": f"/pages/{p.id}/image" if p and p.image_path else ""})


@app.get("/pages/{page_id}/image")
def page_image(page_id: int):
    with SessionLocal() as db:
        page = db.get(Page, page_id)
        if not page or not page.image_path or not data_path(page.image_path).exists():
            raise HTTPException(404)
        return FileResponse(data_path(page.image_path))


def _review(sub_id: int | None, action: dict, missing: str, done: str, anchor: str, back_to: str,
            field: str = "", doc_type: str = "") -> RedirectResponse:
    """Apply a review action (D-51: it resumes the submission's paused graph, or runs the review function
    directly when there is none) and go back to the page: `done` (with {decision}) at `anchor`, or, if refused,
    the reason at `back_to` (the card or value being worked on). `missing`: the error when the target doesn't
    exist (no page to go back to). Traced as `apply-review` (D-52)."""
    if sub_id is None:
        raise _review_error(review.ReviewError(missing))
    with SessionLocal() as db:
        before = db.get(Submission, sub_id).decision
    trace_input = {"kind": action["kind"], "field": field, "doc_type": doc_type}
    if config.LANGFUSE_CAPTURE_CONTENT:
        trace_input.update({k: action[k] for k in ("value", "reason") if k in action})
    refused = None
    with tracing.trace("apply-review", sub_id, input=trace_input, tags=["review"], user_id=REVIEWER) as root:
        try:
            graph.apply_review(sub_id, {**action, "actor": REVIEWER})
        except review.ReviewError as exc:
            refused = exc              # raised after the trace closes, so it's recorded as a warning, not an error
            root.update(level="WARNING", status_message=f"refused: {exc}", output={"refused": str(exc)})
        else:
            with SessionLocal() as db:
                after = db.get(Submission, sub_id).decision
            tracing.score_review(action["kind"], field=field, doc_type=doc_type, decision_before=before,
                                 decision_after=after)
            root.update(output={"decision": after})
    url = f"/submissions/{sub_id}"
    if refused:    # back to the page with the reason, not a bare error response (found clicking through, D-55)
        return _back(url, f"Not saved: {refused}", back_to, error=True)
    return _back(url, done.format(decision=after), anchor)


@app.post("/fields/{field_id}/override")
def override_field(field_id: int, value: str = Form(...), reason: str = Form(...)):
    with SessionLocal() as db:
        f = db.get(Field, field_id)
        sub_id, name, doc_type = (f.submission_id, f.name, f.doc_type) if f else (None, "", "")
    return _review(sub_id, {"kind": "override", "field_id": field_id, "value": value, "reason": reason},
                   "field not found", "Value updated. Rules re-run: {decision}", "#rules", f"#F{field_id}",
                   name, doc_type)


@app.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: int, field_id: int = Form(...), reason: str = Form(...)):
    with SessionLocal() as db:
        c = db.get(Conflict, conflict_id)
        sub_id, name = (c.submission_id, c.field_name) if c else (None, "")
    return _review(sub_id, {"kind": "resolve", "conflict_id": conflict_id, "field_id": field_id, "reason": reason},
                   "conflict not found or not open",
                   ("Acknowledged" if name in ("scheduled", "loss_run") else "Conflict resolved") + ". Rules re-run: {decision}",
                   "#conflicts",
                   f"#C{conflict_id}", name)


@app.post("/submissions/{sub_id}/rerun-rules")
def rerun_rules(sub_id: int):
    return _review(sub_id, {"kind": "rerun_rules"}, "submission not found", "Rules re-run: {decision}", "#rules",
                   "#rules")
