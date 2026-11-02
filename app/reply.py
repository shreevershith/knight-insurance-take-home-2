"""Draft reply to the agent: what the underwriter would send next, written from what the checks found (D-48).

Code decides what goes in (decline reasons, what couldn't be checked, open disagreements, the analyst's actions
for the agent); the model only words it. A draft for the underwriter to edit and send: nothing is sent.
The draft is checked in code: internal codes and ids are removed, and it may not ask about anything the
checks didn't raise (the model is told so; the inputs are listed next to the draft on the page).
"""
import json
import re

from app import llm
from app.db import Conflict, RuleResult, Submission

SYSTEM = """You write a short email from a commercial auto underwriter at Knight Specialty Insurance to the
insurance agent who sent a trucking submission. You get, already decided by checks in code: the decision, the
guideline reasons for a decline, items that couldn't be checked or need verifying, items that need the
underwriter's judgment, documents that disagree, and the analyst's suggested actions for the agent.

Write the email the underwriter would send next:
- decision DECLINE: say Knight can't offer terms under its Trucking Program and give each reason in plain words.
  Don't ask for documents unless the reason says information is missing.
- otherwise: thank the agent, then ask, as a numbered list, only for what would settle the items given
  (a missing document, a clarification, which of two values is right). Group related asks. Don't promise a quote.
Plain business English, no internal rule codes, no ids, no confidence numbers, no mention of AI or a rules engine.
Ask only about items in the data. Under 250 words. Sign off as "Knight Underwriting"."""

SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"],
    "additionalProperties": False,
}

# internal references the agent shouldn't see: rule codes (PRO-03) anywhere, citation ids (F123, C12, R45) when
# bracketed ("(F123, R45)"); a bare F150 may be a truck
_REF = r"(?:[A-Z]{2,3}-\d{2}|[FCR]\d+)"
_CODES = re.compile(rf"\s*\({_REF}(?:[,;/ ]+{_REF})*\)|\b[A-Z]{{2,3}}-\d{{2}}\b")


def asks(db, sub: Submission, analysis: dict) -> dict:
    """What the draft is about, chosen in code from the latest rules run, open conflicts and the analyst."""
    run = db.query(RuleResult).filter(RuleResult.submission_id == sub.id).order_by(RuleResult.id.desc()).first()
    rules = db.query(RuleResult).filter(RuleResult.submission_id == sub.id,
                                        RuleResult.run_id == run.run_id).all() if run else []

    def item(r):
        return {"check": r.title, "finding": r.message.split(" | OK: ")[0]}
    conflicts = db.query(Conflict).filter(Conflict.submission_id == sub.id, Conflict.status == "OPEN").all()
    return {
        "decision": sub.decision,
        "decline_reasons": [item(r) for r in rules if r.outcome == "DECLINE"],
        "could_not_check_or_verify": [item(r) for r in rules if r.outcome in ("NOT_EVALUATED", "VERIFY")],
        "needs_underwriter_judgment": [item(r) for r in rules if r.outcome == "REFER" and r.rule_id != "CONFLICTS"],
        "documents_disagree": [c.description for c in conflicts],
        "analyst_actions_for_agent": [r["action"] for r in analysis.get("recommendations", [])
                                      if r.get("audience") == "agent"],
    }


def draft(db, sub: Submission, analysis: dict, insured: str) -> dict:
    """{'subject', 'body', 'removed_codes'}; raises llm.LLMError."""
    data = asks(db, sub, analysis)
    if not any(v for k, v in data.items() if k != "decision"):
        data["note"] = "nothing is missing or in question: a short note that the submission is complete and under review"
    context = {"insured": insured, "agent": sub.sender, "their_subject": sub.subject, **data}
    out = llm.structured_call(
        task="agent_reply", system=SYSTEM, schema=SCHEMA, max_tokens=2000,
        content=[{"type": "text", "text": "<submission_findings>\n" + json.dumps(context, default=str) +
                  "\n</submission_findings>"}], ctx={"submission_id": sub.id})
    body, n = _CODES.subn("", out["body"])
    subject, m = _CODES.subn("", out["subject"])
    return {"subject": subject.strip(), "body": body.strip(), "removed_codes": n + m}
