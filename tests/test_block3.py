"""Block 3: patterns in YAML, portable paths and DB URL, one job per submission, stale AI findings."""
import copy

import pytest

from app import config, pipeline, review
from app.db import Document, Field, Page, SessionLocal, Submission, init_db
from app.rules import engine


def test_patterns_live_in_yaml_and_are_validated():
    cfg = engine.load_config()
    assert cfg["patterns"]["border_states"] == ["CA", "AZ", "NM", "TX"]
    assert "lithium" in cfg["patterns"]["commodity_prohibited"]["PRO-02"]
    bad = copy.deepcopy({k: v for k, v in cfg.items() if not k.startswith("_")})
    bad["patterns"]["commodity_prohibited"]["PRO-08"] = "waste|("   # broken regex
    del bad["patterns"]["cdl_event"]
    errors = engine.validate_config(bad)
    assert any("PRO-08: invalid regex" in e for e in errors)
    assert any("'cdl_event' is required" in e for e in errors)


def test_paths_are_stored_relative_to_the_data_dir():
    p = config.BLOB_DIR / "abc.pdf"
    stored = config.stored_path(p)
    assert stored == "blobs/abc.pdf"
    assert config.data_path(stored) == config.DATA_DIR / "blobs" / "abc.pdf"
    assert config.data_path(str(p.resolve())) == p.resolve()           # older rows: absolute paths


@pytest.fixture()
def sub_id():
    init_db()
    with SessionLocal() as db:
        s = Submission(subject="block3", status="DONE", summary={"summary": "ok", "concerns": [], "recommendations": []})
        db.add(s)
        db.flush()
        d = Document(submission_id=s.id, filename="a.pdf", sha256="b3", blob_path="blobs/a.pdf", status="OK")
        db.add(d)
        db.flush()
        page = Page(document_id=d.id, page_no=1, doc_type="APPLICATION", status="OK", text="DOT 123")
        db.add(page)
        db.flush()
        db.add(Field(submission_id=s.id, page_id=page.id, doc_type="APPLICATION", entity_type="applicant",
                     entity_key="applicant", name="dot_number", value="123", method="llm_text",
                     confidence=0.9, status="OK"))
        db.commit()
        return s.id


def test_one_active_job_per_submission(sub_id):
    assert pipeline.claim(sub_id) is True
    assert pipeline.claim(sub_id) is False          # a second click / worker does not start a 2nd job
    with SessionLocal() as db:
        f = db.query(Field).filter_by(submission_id=sub_id).first()
        with pytest.raises(review.ReviewError, match="being processed"):
            review.override_field(db, f.id, "456", "typo", "uw")
        with pytest.raises(review.ReviewError, match="being processed"):
            review.rerun_rules(db, sub_id, "uw")


def test_restart_marks_cut_off_jobs_interrupted(sub_id):
    pipeline.claim(sub_id)
    assert pipeline.reset_interrupted() >= 1
    with SessionLocal() as db:
        assert db.get(Submission, sub_id).status == "INTERRUPTED"
    assert pipeline.claim(sub_id) is True           # and it can be reprocessed


def test_review_change_marks_ai_findings_out_of_date(sub_id):
    with SessionLocal() as db:
        f = db.query(Field).filter_by(submission_id=sub_id).first()
        sub = review.override_field(db, f.id, "456", "agent corrected the DOT number", "uw")
        db.commit()
        assert "applicant.dot_number" in sub.summary["out_of_date"]
        assert sub.summary["summary"] == "ok"        # findings kept, just labelled


def test_rerun_marks_ai_findings_out_of_date_only_when_an_outcome_changes(sub_id):
    with SessionLocal() as db:
        sub = db.get(Submission, sub_id)
        sub.summary = {"summary": "written earlier"}
        db.commit()
        review.rerun_rules(db, sub_id, "uw")        # first run: outcomes appear where there were none
        sub.summary = {"summary": "written after that run"}
        db.commit()
        review.rerun_rules(db, sub_id, "uw")        # same values, same rules: nothing the summary used changed
        db.commit()
        assert "out_of_date" not in db.get(Submission, sub_id).summary
        f = db.query(Field).filter_by(submission_id=sub_id, name="dot_number").first()
        db.add(Field(submission_id=sub_id, page_id=f.page_id, doc_type="APPLICATION", entity_type="operations",
                     entity_key="operations", name="hazmat", value="yes", method="llm_text", confidence=0.9,
                     status="OK"))                    # hazmat answered Yes: PRO-01 now declines
        db.commit()
        review.rerun_rules(db, sub_id, "uw")
        db.commit()
        assert "outcome changed" in db.get(Submission, sub_id).summary.get("out_of_date", "")
