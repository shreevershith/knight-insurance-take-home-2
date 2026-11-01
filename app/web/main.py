"""Underwriter web app."""
import threading
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from urllib.parse import quote

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import config, pipeline, review
from app.config import data_path
from app.db import (AuditEvent, Conflict, Document, Field, JobLog, Page, RuleResult, SessionLocal, Submission,
                    audit, init_db)
from app.intake import ingest_eml, ingest_files
from app.log import setup_logging

setup_logging()
init_db()
pipeline.reset_interrupted()  # nothing runs before startup: anything still PROCESSING was cut off
app = FastAPI(title="Knight Commercial Auto Intake")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
REVIEWER = "underwriter@knight"  # prototype: no SSO; production uses the signed-in user (RBAC)


def _start(submission_id: int, job=pipeline.run) -> None:
    if not pipeline.claim(submission_id):
        raise HTTPException(409, "this submission is already being processed")
    threading.Thread(target=job, args=(submission_id, True), daemon=True).start()


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


def _back(url: str, message: str, anchor: str = "") -> RedirectResponse:
    """Redirect after an action, carrying a confirmation shown as a toast."""
    return RedirectResponse(f"{url}?ok={quote(message)}{anchor}", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with SessionLocal() as db:
        subs = db.query(Submission).order_by(Submission.id.desc()).all()
        return templates.TemplateResponse(request, "index.html", {
            "subs": subs, "llm_ok": config.llm_configured(),
            "routes": {t: config.route(t)[0].replace("claude-", "") for t in config.TIERS}})


@app.post("/submissions")
async def create_submission(files: list[UploadFile]):
    payload = [(f.filename or "file", await f.read()) for f in files if f.filename]
    if not payload:
        raise HTTPException(400, "no files")
    with SessionLocal() as db:
        if len(payload) == 1 and payload[0][0].lower().endswith(".eml"):
            sub = ingest_eml(db, payload[0][1])
        else:
            sub = ingest_files(db, payload, sender="upload", subject="Uploaded files")
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
    _start(sub_id, pipeline.run_analysis)
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
        last = db.query(RuleResult).filter(RuleResult.submission_id == sub_id).order_by(RuleResult.id.desc()).first()
        rules = (db.query(RuleResult).filter(RuleResult.submission_id == sub_id, RuleResult.run_id == last.run_id)
                 .order_by(RuleResult.id).all()) if last else []
        audits = db.query(AuditEvent).filter(AuditEvent.submission_id == sub_id).order_by(AuditEvent.id.desc()).all()
        entities = defaultdict(list)
        for f in fields:
            entities[f.entity_key].append(f)
        field_by_id = {f.id: f for f in fields}
        pages = {p.id: p for d in docs for p in d.pages}
        return templates.TemplateResponse(request, "submission.html", {
            "sub": sub, "docs": docs, "logs": logs, "entities": dict(entities), "conflicts": conflicts,
            "rules": rules, "audits": audits, "replaced": replaced, "field_by_id": field_by_id, "pages": pages,
            "running": sub.status in ("RECEIVED", "PROCESSING"), "stages": STAGES, "stage": _stage(logs),
            "low_confidence": sum(f.status == "LOW_CONFIDENCE" for f in fields),
            "toast": request.query_params.get("ok", "")})


@app.get("/submissions/{sub_id}/status")
def status(sub_id: int, after: int = 0):
    """Polled by the page while a job runs: status, current stage, and new log lines."""
    with SessionLocal() as db:
        sub = db.get(Submission, sub_id)
        if not sub:
            raise HTTPException(404)
        logs = db.query(JobLog).filter(JobLog.submission_id == sub_id).order_by(JobLog.id).all()
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
            "method": f.method, "confidence": f.confidence, "status": f.status, "checks": f.validation or {},
            "source": f"{p.document.filename} · page {p.page_no}" if p else "",
            "image": f"/pages/{p.id}/image" if p and p.image_path else ""})


@app.get("/pages/{page_id}/image")
def page_image(page_id: int):
    with SessionLocal() as db:
        page = db.get(Page, page_id)
        if not page or not page.image_path or not data_path(page.image_path).exists():
            raise HTTPException(404)
        return FileResponse(data_path(page.image_path))


@app.post("/fields/{field_id}/override")
def override_field(field_id: int, value: str = Form(...), reason: str = Form(...)):
    with SessionLocal() as db:
        try:
            sub = review.override_field(db, field_id, value, reason, REVIEWER)
        except review.ReviewError as exc:
            raise HTTPException(409 if "being processed" in str(exc) else 400, str(exc))
        db.commit()
        return _back(f"/submissions/{sub.id}", f"Value updated. Rules re-run: {sub.decision}", "#rules")


@app.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: int, field_id: int = Form(...), reason: str = Form(...)):
    with SessionLocal() as db:
        try:
            sub = review.resolve_conflict(db, conflict_id, field_id, reason, REVIEWER)
        except review.ReviewError as exc:
            raise HTTPException(409 if "being processed" in str(exc) else 400, str(exc))
        db.commit()
        return _back(f"/submissions/{sub.id}", f"Conflict resolved. Rules re-run: {sub.decision}", "#conflicts")


@app.post("/submissions/{sub_id}/rerun-rules")
def rerun_rules(sub_id: int):
    with SessionLocal() as db:
        try:
            review.rerun_rules(db, sub_id, REVIEWER)
        except review.ReviewError as exc:
            raise HTTPException(409, str(exc))
        db.commit()
        decision = db.get(Submission, sub_id).decision
    return _back(f"/submissions/{sub_id}", f"Rules re-run: {decision}", "#rules")
