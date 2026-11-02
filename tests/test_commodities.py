"""Commodities sorted by the model against the prohibited list (D-49): the model's category and the keyword are
compared in the rules; a person's category decides; a failed sort leaves the keyword check as it was."""
import pytest

from app import commodities, llm
from app.db import Document, Field, Page, RuleResult, SessionLocal, Submission, init_db
from app.rules import engine

APP = [("applicant", "application_date", "10/22/2026"), ("applicant", "mailing_state", "CA")]


@pytest.fixture()
def db():
    init_db()
    session = SessionLocal()
    yield session
    session.close()


def _sub(db, commodity_rows, categories=()):
    """commodity_rows: [(name, percent)]; categories: [(index, value, confidence, method)]."""
    s = Submission(subject="commodities")
    db.add(s)
    db.flush()
    d = Document(submission_id=s.id, filename="app.pdf", sha256=f"c{s.id}", blob_path="x", status="OK")
    db.add(d)
    db.flush()
    p = Page(document_id=d.id, page_no=1, doc_type="APPLICATION", status="OK", text="")
    db.add(p)
    db.flush()

    def add(key, name, value, method="llm_text", conf=0.9):
        db.add(Field(submission_id=s.id, page_id=p.id, doc_type="APPLICATION", entity_type=key.split(":")[0],
                     entity_key=key, name=name, value=value, method=method, confidence=conf,
                     status="OK" if conf >= 0.85 else "LOW_CONFIDENCE"))
    for key, name, value in APP:
        add(key, name, value)
    for i, (name, pct) in enumerate(commodity_rows):
        add(f"commodity:app:{i}", "commodity", name)
        add(f"commodity:app:{i}", "percent", pct)
    for i, value, conf, method in categories:
        add(f"commodity:app:{i}", "prohibited_category", value, method, conf)
    db.commit()
    return s


def _rules(db, s):
    engine.evaluate(db, s)
    db.commit()
    last = db.query(RuleResult).filter_by(submission_id=s.id).order_by(RuleResult.id.desc()).first()
    return {r.rule_id: r for r in db.query(RuleResult).filter_by(submission_id=s.id, run_id=last.run_id)}


def test_model_and_keyword_agree_declines(db):
    s = _sub(db, [("Lithium-ion batteries", "10"), ("Lumber", "90")], [(0, "lithium batteries", 0.97, "llm_category"),
                                                                        (1, "none", 0.98, "llm_category")])
    assert _rules(db, s)["PRO-02"].outcome == "DECLINE"


def test_model_finds_what_no_keyword_names(db):
    s = _sub(db, [("Spent Li-ion cells for disposal", "10"), ("Lumber", "90")],
             [(0, "lithium batteries", 0.95, "llm_category"), (1, "none", 0.98, "llm_category")])
    assert _rules(db, s)["PRO-02"].outcome == "DECLINE"


def test_an_unsure_model_cannot_decline_on_its_own(db):
    s = _sub(db, [("Electronics cells", "10"), ("Lumber", "90")],
             [(0, "lithium batteries", 0.6, "llm_category"), (1, "none", 0.98, "llm_category")])
    assert _rules(db, s)["PRO-02"].outcome == "REFER"          # low-confidence category: the guard


def test_keyword_hit_the_model_disputes_is_confirmed_not_passed(db):
    s = _sub(db, [("Scrap lumber offcuts for furniture makers", "100")], [(0, "none", 0.9, "llm_category")])
    r = _rules(db, s)["PRO-08"]
    assert r.outcome == "NOT_EVALUATED" and "confirm what is hauled" in r.message


def test_a_persons_category_decides(db):
    s = _sub(db, [("Scrap lumber offcuts for furniture makers", "100")], [(0, "None", 1.0, "human")])
    assert _rules(db, s)["PRO-08"].outcome == "PASS"
    s = _sub(db, [("Furniture", "100")], [(0, "Waste", 1.0, "human")])
    assert _rules(db, s)["PRO-08"].outcome == "DECLINE"


def test_without_a_category_the_keyword_check_is_unchanged(db):
    s = _sub(db, [("Towing services", "100")])
    assert _rules(db, s)["PRO-06"].outcome == "DECLINE"
    s = _sub(db, [("Paper towels and tissue", "100")])
    assert _rules(db, s)["PRO-06"].outcome == "PASS"


def test_sorting_writes_one_category_per_commodity(db, monkeypatch):
    s = _sub(db, [("Paper towels", "50"), ("Lithium batteries", "50")])
    monkeypatch.setattr(llm, "structured_call", lambda **kw: {"items": [
        {"name": "paper towels", "category": "none", "reason": "Paper goods.", "confidence": 0.98},
        {"name": "Lithium batteries", "category": "lithium batteries", "reason": "Named.", "confidence": 0.7}]})
    assert commodities.classify(db, s) == 2
    got = {f.entity_key: (f.value, f.status, f.method) for f in db.query(Field).filter_by(
        submission_id=s.id, name="prohibited_category")}
    assert got == {"commodity:app:0": ("none", "OK", "llm_category"),
                   "commodity:app:1": ("lithium batteries", "LOW_CONFIDENCE", "llm_category")}
