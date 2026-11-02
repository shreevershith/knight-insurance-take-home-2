"""Draft reply to the agent (D-48): code picks what it is about, the model only words it, codes are stripped,
and a failed draft never costs the analysis."""
import pytest

from app import analyst, llm, reply
from app.db import Conflict, Document, Field, Page, RuleResult, SessionLocal, Submission, init_db


@pytest.fixture()
def sub_id():
    init_db()
    with SessionLocal() as db:
        s = Submission(subject="Cobalt - new business", sender="Agent <a@x.example>", status="DONE", decision="REFER")
        db.add(s)
        db.flush()
        d = Document(submission_id=s.id, filename="app.pdf", sha256="reply1", blob_path="blobs/a.pdf", status="OK")
        db.add(d)
        db.flush()
        p = Page(document_id=d.id, page_no=1, doc_type="APPLICATION", status="OK", text="")
        db.add(p)
        db.flush()
        db.add(Field(submission_id=s.id, page_id=p.id, doc_type="APPLICATION", entity_type="applicant",
                     entity_key="applicant", name="business_name", value="ACME HAULING LLC", method="llm_text",
                     confidence=0.9, status="OK"))
        for rid, outcome, title in [("PRO-02", "DECLINE", "No lithium batteries"), ("DRV-05", "NOT_EVALUATED", "Medical card"),
                                    ("DRV-06", "REFER", "Driver points"), ("CONFLICTS", "REFER", "No open conflicts"),
                                    ("SUB-01", "PASS", "FEIN present")]:
            db.add(RuleResult(submission_id=s.id, run_id="r1", rule_id=rid, rules_version="v1", title=title,
                              outcome=outcome, message=f"{title} finding | OK: others fine"))
        db.add(Conflict(submission_id=s.id, kind="CONFLICT", entity_key="applicant", field_name="agency_phone",
                        description="Agency phone differs", values=[], suggestion_reason="", status="OPEN", resolution=""))
        db.add(Conflict(submission_id=s.id, kind="CONFLICT", entity_key="applicant", field_name="fein",
                        description="FEIN differs", values=[], suggestion_reason="", status="RESOLVED", resolution="x"))
        db.commit()
        return s.id


def test_code_chooses_what_the_draft_is_about(sub_id):
    with SessionLocal() as db:
        a = reply.asks(db, db.get(Submission, sub_id),
                       {"recommendations": [{"action": "Ask for the 2022 loss run", "audience": "agent"},
                                            {"action": "Check SAFER", "audience": "underwriter"}]})
    assert [i["check"] for i in a["decline_reasons"]] == ["No lithium batteries"]
    assert [i["check"] for i in a["could_not_check_or_verify"]] == ["Medical card"]
    assert [i["check"] for i in a["needs_underwriter_judgment"]] == ["Driver points"]   # not the CONFLICTS rule
    assert a["decline_reasons"][0]["finding"] == "No lithium batteries finding"         # passing part dropped
    assert a["documents_disagree"] == ["Agency phone differs"]                           # open ones only
    assert a["analyst_actions_for_agent"] == ["Ask for the 2022 loss run"]


def test_internal_codes_are_removed_but_a_truck_model_is_not(sub_id, monkeypatch):
    monkeypatch.setattr(llm, "structured_call", lambda **kw: {
        "subject": "Acme Hauling (PRO-02)",
        "body": "Lithium batteries are excluded (PRO-02, F12). Please confirm the F150 pickup. See DRV-05."})
    with SessionLocal() as db:
        out = reply.draft(db, db.get(Submission, sub_id), {"recommendations": []}, "ACME HAULING LLC")
    assert out["subject"] == "Acme Hauling"
    assert out["body"] == "Lithium batteries are excluded. Please confirm the F150 pickup. See ."
    assert out["removed_codes"] == 3


def test_a_failed_draft_keeps_the_analysis(sub_id, monkeypatch):
    def fake(**kw):
        if kw["task"] == "agent_reply":
            raise llm.LLMError("timeout")
        return {"summary": "ok", "concerns": [], "recommendations": []}
    monkeypatch.setattr(llm, "structured_call", fake)
    monkeypatch.setattr(analyst, "build_context", lambda db, sub: ({"entities": {}}, set()))
    with SessionLocal() as db:
        data = analyst.analyze(db, db.get(Submission, sub_id))
    assert data["summary"] == "ok"
    assert data["agent_reply"] == {"error": "MODEL_ERROR: timeout"}
