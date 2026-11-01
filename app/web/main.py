"""Underwriter web app."""
import threading
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with SessionLocal() as db:
        subs = db.query(Submission).order_by(Submission.id.desc()).all()
        return templates.TemplateResponse(request, "index.html", {
            "subs": subs, "llm_ok": config.llm_configured(), "model": config.CLAUDE_MODEL})


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
    return RedirectResponse(f"/submissions/{sub_id}", status_code=303)


@app.post("/submissions/{sub_id}/reprocess")
def reprocess(sub_id: int):
    _start(sub_id)
    with SessionLocal() as db:
        audit(db, sub_id, REVIEWER, "submission.reprocess", f"submission:{sub_id}")
        db.commit()
    return RedirectResponse(f"/submissions/{sub_id}", status_code=303)


@app.post("/submissions/{sub_id}/rerun-analysis")
def rerun_analysis(sub_id: int):
    _start(sub_id, pipeline.run_analysis)
    with SessionLocal() as db:
        audit(db, sub_id, REVIEWER, "analysis.rerun", f"submission:{sub_id}")
        db.commit()
    return RedirectResponse(f"/submissions/{sub_id}#ai", status_code=303)


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
            "running": sub.status in ("RECEIVED", "PROCESSING")})


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
        return RedirectResponse(f"/submissions/{sub.id}#rules", status_code=303)


@app.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: int, field_id: int = Form(...), reason: str = Form(...)):
    with SessionLocal() as db:
        try:
            sub = review.resolve_conflict(db, conflict_id, field_id, reason, REVIEWER)
        except review.ReviewError as exc:
            raise HTTPException(409 if "being processed" in str(exc) else 400, str(exc))
        db.commit()
        return RedirectResponse(f"/submissions/{sub.id}#conflicts", status_code=303)


@app.post("/submissions/{sub_id}/rerun-rules")
def rerun_rules(sub_id: int):
    with SessionLocal() as db:
        try:
            review.rerun_rules(db, sub_id, REVIEWER)
        except review.ReviewError as exc:
            raise HTTPException(409, str(exc))
        db.commit()
    return RedirectResponse(f"/submissions/{sub_id}#rules", status_code=303)
