"""AI analyst: summary, underwriting concerns, recommendations. Advisory only (D-12).

The model sees the resolved fields, conflicts and rule outcomes, and must cite the ids it relies on.
Citations are checked in code: an id that doesn't exist is removed and the item is marked.
"""
import json

from app import llm
from app.conflicts import active_fields
from app.db import Conflict, RuleResult, Submission

SYSTEM = """You are a senior commercial auto (trucking) underwriting analyst reviewing a submission
that has already been extracted and checked by a deterministic rules engine.

Your job is advisory: summarize the account, identify underwriting concerns using industry knowledge
(loss history, driver quality, operations, fleet growth, compliance signals, radius, commodities), and
recommend next actions for the underwriter and the agent. You do NOT make the accept/decline decision;
the rules engine does. Do not restate every rule result; add judgment the rules cannot.

Every concern and recommendation must cite the ids it is based on: field ids (F123), conflict ids (C12)
or rule result ids (R45), taken only from the data provided. Do not cite anything not in the data.
confidence is 0-1: how strongly the cited evidence supports the item. Be concise and specific."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "detail": {"type": "string"},
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"}},
            "required": ["title", "detail", "severity", "evidence", "confidence"], "additionalProperties": False}},
        "recommendations": {"type": "array", "items": {"type": "object", "properties": {
            "action": {"type": "string"}, "audience": {"type": "string", "enum": ["underwriter", "agent", "operations"]},
            "reason": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"}},
            "required": ["action", "audience", "reason", "evidence", "confidence"], "additionalProperties": False}},
    },
    "required": ["summary", "concerns", "recommendations"],
    "additionalProperties": False,
}


def build_context(db, sub: Submission) -> tuple[dict, set[str]]:
    fields = active_fields(db, sub.id)
    conflicts = db.query(Conflict).filter(Conflict.submission_id == sub.id, Conflict.status != "REPLACED").all()
    run = db.query(RuleResult).filter(RuleResult.submission_id == sub.id).order_by(RuleResult.id.desc()).first()
    rules = db.query(RuleResult).filter(RuleResult.submission_id == sub.id,
                                        RuleResult.run_id == run.run_id).all() if run else []
    ids = set()
    entities = {}
    for f in fields:
        ids.add(f"F{f.id}")
        src = f"{f.page.document.filename} p{f.page.page_no}" if f.page else "human override"
        entities.setdefault(f.entity_key, []).append(
            {"id": f"F{f.id}", "field": f.name, "value": f.value, "source": src, "confidence": f.confidence})
    ids |= {f"C{c.id}" for c in conflicts} | {f"R{r.id}" for r in rules}
    from app.rules import derived
    from app.rules.view import SubmissionView
    view = SubmissionView(db, sub.id)
    states, _ = derived.operating_states(view)
    tiv = derived.computed_tiv(view)
    cdl = {derived.driver_label(view, k): [f"{e.on:%m/%d/%Y}: {e.what}" for e in derived.cdl_evidence(view, k)]
           for k in derived.roster(view)}
    context = {
        "decision_by_rules": sub.decision, "routing": sub.team,
        "computed_in_code": {
            "tiv_cost_new_total": tiv["cost_new_total"], "stated_value_total": tiv["stated_value_total"],
            "units_missing_stated_value": tiv["units_missing_stated_value"],
            "operating_states_from_ifta": sorted(states), "cdl_evidence_by_driver": cdl},
        "rule_results": [{"id": f"R{r.id}", "rule": r.rule_id, "outcome": r.outcome, "message": r.message} for r in rules],
        "conflicts": [{"id": f"C{c.id}", "kind": c.kind, "entity": c.entity_key, "field": c.field_name,
                       "status": c.status, "values": [{"value": v["value"], "source": v["source"]} for v in c.values]}
                      for c in conflicts],
        "entities": entities,
    }
    return context, ids


def analyze(db, sub: Submission) -> dict:
    context, valid_ids = build_context(db, sub)
    data = llm.structured_call(
        task="analyst", system=SYSTEM, schema=SCHEMA, max_tokens=16000,
        content=[{"type": "text", "text": "<submission_data>\n" + json.dumps(context, default=str) +
                  "\n</submission_data>"}], ctx={"submission_id": sub.id})
    for item in data["concerns"] + data["recommendations"]:
        cited = [e.strip() for e in item.get("evidence", [])]
        item["evidence"] = [e for e in cited if e in valid_ids]
        item["invalid_citations"] = [e for e in cited if e not in valid_ids]
        if not item["evidence"]:
            item["confidence"] = min(item.get("confidence", 0), 0.3)
            item["unsupported"] = True
    return data
