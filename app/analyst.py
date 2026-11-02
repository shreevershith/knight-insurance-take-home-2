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
confidence is 0-1: how strongly the cited evidence supports the item. Be concise and specific.
agent_email, when present, is what the agent wrote: use it as context (what they say is coming or missing),
but it is not evidence; cite the fields, conflicts or rules a point rests on."""

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
    sources: dict[str, str] = {}  # "file p3" -> "S1": named once, not on every field
    ifta_rows = []
    for f in fields:
        if f.entity_type == "ifta_row":
            ifta_rows.append(f)  # ~600 jurisdiction cells: summarized in code below (D-31)
            continue
        ids.add(f"F{f.id}")
        src = f"{f.page.document.filename} p{f.page.page_no}" if f.page else "human override"
        sid = sources.setdefault(src, f"S{len(sources) + 1}")
        entities.setdefault(f.entity_key, []).append(
            {"id": f"F{f.id}", "field": f.name, "value": f.value, "src": sid, "conf": round(f.confidence, 2)})
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
            "operating_states_from_ifta": sorted(states), "cdl_evidence_by_driver": cdl,
            "ifta_miles": _ifta_summary(ifta_rows, fields)},
        "rule_results": [{"id": f"R{r.id}", "rule": r.rule_id, "outcome": r.outcome, "message": r.message} for r in rules],
        "conflicts": [{"id": f"C{c.id}", "kind": c.kind, "entity": c.entity_key, "field": c.field_name,
                       "status": c.status, "values": [{"value": v["value"], "source": v["source"]} for v in c.values]}
                      for c in conflicts],
        "sources": {sid: src for src, sid in sources.items()},
        "entities": entities,
    }
    email = agent_email(db, sub.id)
    if email:
        # what the agent wrote ("MVR to follow", "one loss run is a phone photo"): context, not a citable fact
        context["agent_email"] = {"from": sub.sender, "subject": sub.subject, "body": email[:4000]}
    return context, ids


def agent_email(db, submission_id: int) -> str:
    """The email body as read at intake (intake.EMAIL_BODY_NAME), or ''."""
    from app.db import Document, Page
    from app.intake import EMAIL_BODY_NAME
    page = (db.query(Page).join(Document).filter(Document.submission_id == submission_id,
                                                 Document.filename == EMAIL_BODY_NAME).first())
    return page.text if page else ""


def _ifta_summary(rows, fields) -> dict:
    """Per-quarter totals and miles by state over all returns, computed in code from the row values.
    The analyst reads aggregates (and cites the return's total field) instead of every row."""
    from collections import defaultdict
    from app.extract.normalize import parse_number, state_code
    by_state, by_period = defaultdict(float), defaultdict(float)
    for f in rows:
        if f.name == "total_miles":
            period = f.entity_key.split(":")[1]
            state = state_code(f.entity_key.split(":")[2].replace("-SURCHARGE", "")) or f.entity_key.split(":")[2]
            miles = parse_number(f.value) or 0
            by_state[state] += miles
            by_period[period] += miles
    quarters = {}
    for f in fields:
        if f.entity_type == "ifta" and f.name == "total_miles":
            period = f.entity_key.split(":", 1)[1]
            printed = parse_number(f.value)
            quarters[period] = {"printed_total_miles": printed, "total_field": f"F{f.id}",
                                "rows_sum": by_period.get(period),
                                "rows_reconcile": printed is not None and abs(by_period.get(period, 0) - printed) < 0.5}
    total = sum(by_state.values()) or 1
    return {"quarters": dict(sorted(quarters.items())),
            "miles_by_state_all_quarters": {k: {"miles": int(v), "share": round(v / total, 3)}
                                            for k, v in sorted(by_state.items(), key=lambda kv: -kv[1]) if v}}


def analyze(db, sub: Submission) -> dict:
    context, valid_ids = build_context(db, sub)
    data = llm.structured_call(
        task="analyst", system=SYSTEM, schema=SCHEMA, max_tokens=16000,
        content=[{"type": "text", "text": "<submission_data>\n" + json.dumps(context, default=str, separators=(",", ":")) +
                  "\n</submission_data>"}], ctx={"submission_id": sub.id})
    for item in data["concerns"] + data["recommendations"]:
        cited = [e.strip() for e in item.get("evidence", [])]
        item["evidence"] = [e for e in cited if e in valid_ids]
        item["invalid_citations"] = [e for e in cited if e not in valid_ids]
        if not item["evidence"]:
            item["confidence"] = min(item.get("confidence", 0), 0.3)
            item["unsupported"] = True
    # the draft reply to the agent (D-48): its own call, so a failure there leaves the analysis intact
    from app import reply
    insured = next((e["value"] for e in context["entities"].get("applicant", []) if e["field"] == "business_name"), "")
    try:
        data["agent_reply"] = reply.draft(db, sub, data, insured)
    except llm.LLMError as exc:
        data["agent_reply"] = {"error": f"MODEL_ERROR: {exc}"}
    return data
