"""Block 1 invariants: unknown never DECLINEs or PASSes, low confidence never PASSes, broken rules or
config never take the engine down."""
import pytest

from app.db import Document, Field, Page, RuleResult, SessionLocal, Submission, init_db
from app.rules import engine


def _new_sub(db, fields):
    s = Submission(subject="guards")
    db.add(s)
    db.flush()
    d = Document(submission_id=s.id, filename="app.xlsx", sha256="g", blob_path="x", status="OK")
    db.add(d)
    db.flush()
    p = Page(document_id=d.id, page_no=1, doc_type="APPLICATION", status="OK", text="")
    db.add(p)
    db.flush()
    for key, name, value, status in fields:
        db.add(Field(submission_id=s.id, page_id=p.id, doc_type="APPLICATION", entity_type=key.split(":")[0],
                     entity_key=key, name=name, value=value, method="llm_text",
                     confidence=0.9 if status == "OK" else 0.4, status=status))
    db.commit()
    return s


def _results(db, s):
    last = db.query(RuleResult).filter_by(submission_id=s.id).order_by(RuleResult.id.desc()).first()
    return {r.rule_id: r for r in db.query(RuleResult).filter_by(submission_id=s.id, run_id=last.run_id)}


@pytest.fixture()
def db():
    init_db()
    session = SessionLocal()
    yield session
    session.close()


def test_missing_answers_never_decline(db):
    s = _new_sub(db, [("applicant", "application_date", "10/22/2026", "OK")])  # nothing else answered
    engine.evaluate(db, s)
    r = _results(db, s)
    for rid in ("PRO-01", "PRO-02", "PRO-06", "PRO-07", "PRO-08", "ST-01", "ST-02", "ST-04"):
        assert r[rid].outcome == "NOT_EVALUATED", (rid, r[rid].outcome, r[rid].message)
    assert s.decision != "DECLINE"
    assert r["SUB-01"].outcome == "REFER"  # a REQUIRED item that is absent is still a failure


def test_unclassified_page_means_unread_not_missing(db):
    s = _new_sub(db, [("applicant", "application_date", "10/22/2026", "OK")])
    d = Document(submission_id=s.id, filename="scan.pdf", sha256="u", blob_path="x", status="OK")
    db.add(d)
    db.flush()
    db.add(Page(document_id=d.id, page_no=1, doc_type="OTHER", status="UNCLASSIFIED", text=""))
    db.commit()
    engine.evaluate(db, s)
    r = _results(db, s)
    # the FEIN might be on the page we couldn't type: not "missing", but not determinable
    assert r["SUB-01"].outcome == "NOT_EVALUATED" and "could not be read" in r["SUB-01"].message


def test_low_confidence_never_passes_and_never_declines(db):
    s = _new_sub(db, [("operations", "hazmat", "no", "LOW_CONFIDENCE"),
                      ("operations", "intermodal", "yes", "LOW_CONFIDENCE"),
                      ("applicant", "fein", "12-3456789", "LOW_CONFIDENCE")])
    engine.evaluate(db, s)
    r = _results(db, s)
    assert r["PRO-01"].outcome == "NOT_EVALUATED" and "low-confidence" in r["PRO-01"].message
    assert r["PRO-07"].outcome == "REFER" and "pending review" in r["PRO-07"].message
    assert r["SUB-01"].outcome == "NOT_EVALUATED"


def test_verified_value_beats_low_confidence_value(db):
    s = _new_sub(db, [("operations", "hazmat", "yes", "LOW_CONFIDENCE"), ("operations", "hazmat", "no", "OK")])
    engine.evaluate(db, s)
    assert _results(db, s)["PRO-01"].outcome == "PASS"


def test_invalid_status_is_not_valid(db):
    from app.rules.derived import is_commercial
    s = _new_sub(db, [("applicant", "application_date", "10/22/2026", "OK"),
                      ("driver:X1", "last_name", "DOE", "OK"),
                      ("driver:X1", "license_class", "A", "OK"), ("driver:X1", "license_status", "INVALID", "OK")])
    # roster needs a schedule doc type; reuse APPLICATION (in ROSTER_TYPES); MVR values: mark doc type
    for f in db.query(Field).filter(Field.entity_key == "driver:X1", Field.name.in_(["license_class", "license_status"])):
        f.doc_type = "MVR"
    db.commit()
    engine.evaluate(db, s)
    r = _results(db, s)["DRV-02"]
    assert r.outcome == "REFER" and "INVALID" in r.message
    assert is_commercial("M1", None) is None


def test_crashing_rule_is_error_not_fatal(db, monkeypatch):
    s = _new_sub(db, [])

    def boom(v, cfg):
        raise RuntimeError("bad predicate")
    monkeypatch.setitem(engine.PREDICATES, "PRO-01", boom)
    engine.evaluate(db, s)
    r = _results(db, s)
    assert r["PRO-01"].outcome == "ERROR" and "bad predicate" in r["PRO-01"].message
    assert len(r) > 10  # every other rule still ran


def test_invalid_yaml_falls_back_to_last_good(monkeypatch, tmp_path):
    good = engine.load_config()
    bad = tmp_path / "rules.yaml"
    bad.write_text("version: x\nsettings: {operating_states_mode: sometimes}\nparams: {}\nrules: []\n")
    monkeypatch.setattr(engine, "RULES_FILE", bad)
    cfg = engine.load_config()
    assert cfg["version"] == good["version"] and "operating_states_mode" in cfg["_config_error"]


def test_config_change_changes_version(monkeypatch, tmp_path):
    from app.config import RULES_FILE
    flipped = tmp_path / "rules.yaml"
    flipped.write_text(RULES_FILE.read_text(encoding="utf-8").replace("operating_states_mode: refer",
                                                                       "operating_states_mode: decline"))
    v1 = engine.rules_version(engine.load_config())
    monkeypatch.setattr(engine, "RULES_FILE", flipped)
    v2 = engine.rules_version(engine.load_config())
    assert v1 != v2 and v1.split("+")[0] == v2.split("+")[0]
