"""Underwriter web app."""
import threading
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import config, pipeline
from app.db import (AuditEvent, Conflict, Document, Field, JobLog, Page, RuleResult, SessionLocal, Submission,
                    audit, init_db)
from app.intake import ingest_eml, ingest_files
from app.log import setup_logging
from app.rules.engine import evaluate

setup_logging()
init_db()
app = FastAPI(title="Knight Commercial Auto Intake")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
REVIEWER = "underwriter@knight"  # prototype: no SSO; production uses the signed-in user (RBAC)


def _start(submission_id: int) -> None:
    threading.Thread(target=pipeline.run, args=(submission_id,), daemon=True).start()


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
    with SessionLocal() as db:
        audit(db, sub_id, REVIEWER, "submission.reprocess", f"submission:{sub_id}")
        db.commit()
    _start(sub_id)
    return RedirectResponse(f"/submissions/{sub_id}", status_code=303)


@app.get("/submissions/{sub_id}", response_class=HTMLResponse)
def show(request: Request, sub_id: int):
    with SessionLocal() as db:
        sub = db.get(Submission, sub_id)
        if not sub:
            raise HTTPException(404)
        docs = db.query(Document).filter(Document.submission_id == sub_id).order_by(Document.id).all()
        logs = db.query(JobLog).filter(JobLog.submission_id == sub_id).order_by(JobLog.id).all()
        fields = db.query(Field).filter(Field.submission_id == sub_id).order_by(Field.entity_key, Field.name).all()
        conflicts = db.query(Conflict).filter(Conflict.submission_id == sub_id).order_by(Conflict.id).all()
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
            "rules": rules, "audits": audits, "field_by_id": field_by_id, "pages": pages,
            "running": sub.status in ("RECEIVED", "PROCESSING")})


@app.get("/pages/{page_id}/image")
def page_image(page_id: int):
    with SessionLocal() as db:
        page = db.get(Page, page_id)
        if not page or not page.image_path or not Path(page.image_path).exists():
            raise HTTPException(404)
        return FileResponse(page.image_path)


@app.post("/fields/{field_id}/override")
def override_field(field_id: int, value: str = Form(...), reason: str = Form(...)):
    if not reason.strip():
        raise HTTPException(400, "a reason is required")
    with SessionLocal() as db:
        old = db.get(Field, field_id)
        new = Field(submission_id=old.submission_id, page_id=old.page_id, doc_type=old.doc_type,
                    entity_type=old.entity_type, entity_key=old.entity_key, name=old.name, value=value.strip(),
                    excerpt=f"Override by {REVIEWER}: {reason.strip()}", method="human", confidence=1.0,
                    evidence_verified=True, validation={"human_override": True}, status="OK",
                    overrides_field_id=old.id)
        old.status = "SUPERSEDED"
        db.add(new)
        audit(db, old.submission_id, REVIEWER, "field.override", f"field:{old.id} {old.entity_key}.{old.name}",
              before={"value": old.value}, after={"value": new.value}, reason=reason.strip())
        sub = db.get(Submission, old.submission_id)
        evaluate(db, sub, actor=REVIEWER)
        db.commit()
        return RedirectResponse(f"/submissions/{old.submission_id}#rules", status_code=303)


@app.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: int, field_id: int = Form(...), reason: str = Form(...)):
    if not reason.strip():
        raise HTTPException(400, "a reason is required")
    with SessionLocal() as db:
        c = db.get(Conflict, conflict_id)
        chosen = next((v for v in c.values if v["field_id"] == field_id), None)
        if chosen is None:
            raise HTTPException(400, "field is not one of the conflict's values")
        c.status = "RESOLVED"
        c.resolution = f"Accepted '{chosen['value']}' from {chosen['source']}: {reason.strip()}"
        audit(db, c.submission_id, REVIEWER, "conflict.resolve", f"conflict:{c.id} {c.entity_key}.{c.field_name}",
              before={"status": "OPEN"}, after={"accepted": chosen}, reason=reason.strip())
        # the accepted value becomes the authoritative one for the rules
        accepted = db.get(Field, field_id)
        for v in c.values:
            if v["field_id"] != field_id:
                other = db.get(Field, v["field_id"])
                if other and other.entity_key == accepted.entity_key and other.name == accepted.name:
                    other.status = "SUPERSEDED"
        sub = db.get(Submission, c.submission_id)
        evaluate(db, sub, actor=REVIEWER)
        db.commit()
        return RedirectResponse(f"/submissions/{c.submission_id}#conflicts", status_code=303)


@app.post("/submissions/{sub_id}/rerun-rules")
def rerun_rules(sub_id: int):
    with SessionLocal() as db:
        evaluate(db, db.get(Submission, sub_id), actor=REVIEWER)
        db.commit()
    return RedirectResponse(f"/submissions/{sub_id}#rules", status_code=303)
