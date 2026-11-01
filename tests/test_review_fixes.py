"""The /code-review findings on the build branch: each fix's deciding case."""
import threading

import pytest

from app import llm, pipeline
from app.db import Document, Field, Page, RuleResult, SessionLocal, Submission, init_db
from app.resolve import assign_keys
from app.rules import engine
from tests.test_cost_controls import PAGE_2, PAGE_3
from tests.test_guideline_rules import APP, _evaluate


@pytest.fixture()
def db():
    init_db()
    session = SessionLocal()
    yield session
    session.close()


def _latest(db, sub_id):
    last = db.query(RuleResult).filter_by(submission_id=sub_id).order_by(RuleResult.id.desc()).first()
    return {r.rule_id: r for r in db.query(RuleResult).filter_by(submission_id=sub_id, run_id=last.run_id)}


def test_commodity_decline_cites_only_the_matching_commodity(db):
    rows = APP + [("APPLICATION", "commodity:c:0", "commodity", "Scrap metal"), ("APPLICATION", "commodity:c:0", "percent", "20"),
                  ("APPLICATION", "commodity:c:1", "commodity", "Lumber"), ("APPLICATION", "commodity:c:1", "percent", "80")]
    r, s = _evaluate(db, rows)
    scrap = db.query(Field).filter_by(submission_id=s.id, entity_key="commodity:c:0", name="commodity").one()
    assert r["PRO-08"].outcome == "DECLINE" and r["PRO-08"].input_field_ids == [scrap.id]
    # an unrelated low-confidence value can't soften the decline any more
    lumber_pct = db.query(Field).filter_by(submission_id=s.id, entity_key="commodity:c:1", name="percent").one()
    lumber_pct.status, lumber_pct.confidence = "LOW_CONFIDENCE", 0.4
    db.commit()
    engine.evaluate(db, s)
    db.commit()
    assert _latest(db, s.id)["PRO-08"].outcome == "DECLINE"


def test_regional_alone_is_not_interstate(db):
    r, _ = _evaluate(db, APP + [("APPLICATION", "operations", "radius_summary", "Regional - Central Valley, CA only")])
    assert r["APP-03"].outcome != "PASS"


def test_a_cargo_loss_run_does_not_cover_an_auto_liability_term(db):
    term = [("QUOTE_FORM", "prior_policy:2024-12-15", "term_start", "12/15/2024"),
            ("QUOTE_FORM", "prior_policy:2024-12-15", "term_end", "12/15/2025")]
    cargo = [("LOSS_RUN", "loss_run:MTC:P9", "term_start", "12/15/2024"), ("LOSS_RUN", "loss_run:MTC:P9", "term_end", "12/15/2025")]
    lines = ("QUOTE_FORM", "prior_policy:2024-12-15", "coverage_lines", "LIAB/MTC")
    r, _ = _evaluate(db, APP + term + [lines] + cargo)
    assert r["PRI-03"].outcome == "REFER"
    r, _ = _evaluate(db, APP + term + [lines[:3] + ("MTC",)] + cargo)
    assert r["PRI-03"].outcome == "PASS"            # a cargo-only listing is covered by the cargo loss run


def test_policies_sharing_a_term_stay_separate_entities():
    def policy(i, number):
        return [{"group": "prior", "entity_type": "prior_policy", "index": i, "field": k, "value": v, "excerpt": v}
                for k, v in (("term_start", "12/15/2024"), ("policy_number", number))]
    facts = policy(0, "CA-123") + policy(1, "IM-456")
    assign_keys(facts, {"doc_id": 1}, [])
    assert {f["entity_key"] for f in facts} == {"prior_policy:2024-12-15:CA123", "prior_policy:2024-12-15:IM456"}
    single = policy(0, "CA-123")
    assign_keys(single, {"doc_id": 1}, [])
    assert {f["entity_key"] for f in single} == {"prior_policy:2024-12-15"}   # matches across documents


def test_run_id_is_per_job_thread():
    seen = {}

    def job(name):
        llm.run_id.set(name)
        seen[name] = llm.run_id.get()
    threads = [threading.Thread(target=job, args=(n,)) for n in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"A": "A", "B": "B"} and llm.run_id.get() == ""
    assert llm.tier_for("summarize_x") == "extract"


def _sub_with_pages(db, texts, doc_type):
    s = Submission(subject="fixes")
    db.add(s)
    db.flush()
    d = Document(submission_id=s.id, filename="scan.pdf", sha256=f"fix{s.id}", blob_path="x", status="OK")
    db.add(d)
    db.flush()
    pages = [Page(document_id=d.id, page_no=i + 1, doc_type=doc_type, status="OK", text=t,
                  text_source="vision_transcript", classify_method="llm_vision") for i, t in enumerate(texts)]
    db.add_all(pages)
    db.commit()
    return s, d, pages


def test_empty_cheap_extraction_is_escalated(db, monkeypatch):
    s, d, (page,) = _sub_with_pages(db, ["POLICY CA-1 AUTO LIABILITY"], "LOSS_RUN")
    calls = []

    def fake(page, doc_type, use_image, ctx, escalate=False, reason=""):
        calls.append((escalate, reason))
        return [{"group": "lr", "entity_type": "loss_run", "index": 0, "field": "line", "value": "AUTO LIABILITY",
                 "excerpt": "AUTO LIABILITY"}] if escalate else []
    monkeypatch.setattr(pipeline, "extract_page", fake)
    facts = pipeline._llm_extract(db, s, d, page, {})
    assert calls == [(False, ""), (True, "no values extracted")] and facts


def test_ifta_reread_keeps_the_continuation_page_and_human_decisions(db, monkeypatch):
    s, d, pages = _sub_with_pages(db, [PAGE_2, PAGE_3], "IFTA_RETURN")
    db.add(Field(submission_id=s.id, page_id=pages[0].id, doc_type="IFTA_RETURN", entity_type="ifta",
                 entity_key="ifta:2026-03-31", name="total_miles", value="9", method="llm_vision", status="OK"))
    db.add(Field(submission_id=s.id, page_id=pages[1].id, doc_type="IFTA_RETURN", entity_type="ifta",
                 entity_key="ifta:2026-03-31", name="total_miles", value="2,948", method="human", status="OK"))
    db.commit()

    def reclassify(page, ctx, escalate=False, reason=""):  # the re-read calls the table's 2nd page a continuation
        page.doc_type = "IFTA_RETURN" if page.page_no == 1 else "CONTINUATION"
        page.classify_method = "llm_vision"
    monkeypatch.setattr(pipeline, "classify_page", reclassify)
    pipeline._escalate_ifta(db, s, ["2026-03-31"], [])
    db.commit()
    assert pages[1].doc_type == "IFTA_RETURN" and pages[1].status == "OK"
    page2 = db.query(Field).filter(Field.page_id == pages[1].id, Field.method != "human",
                                   Field.status != "REPLACED").all()
    assert any(f.entity_key == "ifta_row:2026-03-31:TX" for f in page2)            # its rows were read again
    total = [f for f in page2 if f.entity_key == "ifta:2026-03-31" and f.name == "total_miles"]
    assert total and all(f.status == "SUPERSEDED" for f in total)                   # the human value still wins
