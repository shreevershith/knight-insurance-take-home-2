"""Block 2: reprocess never destroys human work or history (soft delete + re-apply by fact key)."""
import pytest

from app import conflicts, pipeline, review
from app.db import AuditEvent, Conflict, Document, Field, Page, RuleResult, SessionLocal, Submission, init_db
from app.rules import engine
from app.rules.view import SubmissionView


def _extract_run(db, s, pages, detect=True):
    """Simulates one extraction run producing the same values the sample would."""
    def add(doc_type, key, etype, **values):
        for name, value in values.items():
            db.add(Field(submission_id=s.id, page_id=pages[doc_type].id, doc_type=doc_type, entity_type=etype,
                         entity_key=key, name=name, value=value, method="llm_text", confidence=0.95, status="OK"))
    add("APPLICATION", "applicant", "applicant", business_name="COBALT MESA TRANSPORT INC",
        application_date="10/22/2026", mailing_state="CA")
    add("IFTA_RETURN", "ifta:2025-12-31", "ifta", taxpayer_name="COBALT MESA TRANSPORT LLC")
    add("DRIVER_SCHEDULE", "driver:M5520814", "driver", last_name="MORALES", dob="10/03/1991", license_number="M5520814")
    add("MVR", "driver:M5520814", "driver", dob="10/30/1991", license_class="A COMMERCIAL", license_status="VALID")
    add("QUOTE_FORM", "prior_policy:2022-03-08", "prior_policy", term_start="03/08/2022", term_end="02/10/2023",
        carrier="LAKESHORE MUTUAL CASUALTY", loss_runs_note="LOSS RUNS ATTACHED")
    db.flush()
    if detect:
        conflicts.detect(db, s.id)
        engine.evaluate(db, s)
    db.commit()


@pytest.fixture()
def run1():
    init_db()
    db = SessionLocal()
    s = Submission(subject="reprocess")
    db.add(s)
    db.flush()
    pages = {}
    for t in ("APPLICATION", "IFTA_RETURN", "DRIVER_SCHEDULE", "MVR", "QUOTE_FORM"):
        d = Document(submission_id=s.id, filename=f"{t}.pdf", sha256=t, blob_path="x", status="OK")
        db.add(d)
        db.flush()
        pages[t] = Page(document_id=d.id, page_no=1, doc_type=t, status="OK", text="")
        db.add(pages[t])
    db.flush()
    _extract_run(db, s, pages)
    yield db, s, pages
    db.close()


def _open(db, s):
    return db.query(Conflict).filter_by(submission_id=s.id, status="OPEN").all()


def _human_review(db, s):
    by = {(c.entity_key, c.field_name): c for c in _open(db, s)}
    name = by[("applicant", "business_name")]
    llc = next(v for v in name.values if "LLC" in v["value"])
    review.resolve_conflict(db, name.id, llc["field_id"], "Secretary of State shows LLC", "uw@knight")
    dob = by[("driver:M5520814", "dob")]
    mvr = next(v for v in dob.values if v["doc_type"] == "MVR")
    review.override_field(db, mvr["field_id"], "10/30/1991", "MVR is the state record", "uw@knight")
    inv = by[("prior_policy:2022-03-08", "loss_run")]
    review.resolve_conflict(db, inv.id, inv.values[0]["field_id"], "Agent emailed the Lakeshore loss run", "uw@knight")
    db.commit()


def test_resolving_an_aliased_conflict_changes_what_rules_read(run1):
    db, s, _ = run1
    _human_review(db, s)
    assert SubmissionView(db, s.id).value("applicant", "business_name").raw == "COBALT MESA TRANSPORT LLC"


def test_reprocess_keeps_human_decisions_and_history(run1):
    db, s, pages = run1
    _human_review(db, s)
    decision_before, open_before = s.decision, len(_open(db, s))
    runs_before = db.query(RuleResult.run_id).filter_by(submission_id=s.id).distinct().count()
    first_run_ids = [f.id for f in db.query(Field).filter_by(submission_id=s.id) if f.method != "human"]

    pipeline.reset_for_reprocess(db, s)
    _extract_run(db, s, pages, detect=False)        # run 2 re-extracts the very same values
    pipeline.reapply_human_decisions(db, s.id)      # same order as pipeline.run: re-apply, then detect
    conflicts.detect(db, s.id)
    engine.evaluate(db, s)
    db.commit()

    v = SubmissionView(db, s.id)
    assert v.value("applicant", "business_name").raw == "COBALT MESA TRANSPORT LLC"   # resolution survived
    assert v.value("driver:M5520814", "dob").raw == "10/30/1991"                     # override survived
    assert len(_open(db, s)) == open_before                                          # nothing reopened
    assert s.decision == decision_before
    # nothing was deleted: first-run rows still exist (REPLACED), overrides still point at real rows
    assert all(db.get(Field, i) is not None for i in first_run_ids)
    for h in db.query(Field).filter_by(submission_id=s.id, method="human"):
        assert h.overrides_field_id is None or db.get(Field, h.overrides_field_id) is not None
    assert db.query(RuleResult.run_id).filter_by(submission_id=s.id).distinct().count() > runs_before
    assert db.query(AuditEvent).filter_by(submission_id=s.id, action="conflict.resolve").count() == 2
