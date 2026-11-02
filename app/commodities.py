"""Commodities sorted against the guideline's prohibited cargo by the model, not only by keyword (D-49).

Keywords can't tell "paper towels" from towing or "spent batteries for recycling" from a battery that isn't
named that way. One cheap call reads every commodity name and puts it in a category, with a reason. The result
is stored as a value on the commodity (`prohibited_category`), so it shows on the page, can be corrected by the
underwriter, and survives a reprocess like any other value. The rules still decide (engine._commodity_check):
model and keyword are compared there, and a category below the review threshold is LOW_CONFIDENCE, which the
central guard keeps from declining on its own.
"""
from app import llm
from app.db import Field, Submission
from app.extract.evidence import REVIEW_THRESHOLD

CATEGORIES = {
    "lithium batteries": "lithium batteries or other batteries carried as cargo",
    "towing or recovery": "towing, wrecker or vehicle recovery work",
    "waste": "waste, garbage, refuse, scrap or recycling hauled for disposal",
    "none": "none of these",
}
FIELD = "prohibited_category"
METHOD = "llm_category"

SYSTEM = """You sort the commodities a trucking company hauls against an insurer's prohibited-cargo list.
Categories:
""" + "\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items()) + """

Judge what is actually hauled, not shared letters: paper towels are not towing, pancake batter is not batteries,
recycled-paper products are finished goods, not waste. Return every commodity exactly once, with its name as
given, one category, a one-sentence reason, and confidence 0-1 that the category is right."""

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "category": {"type": "string", "enum": list(CATEGORIES)},
        "reason": {"type": "string"}, "confidence": {"type": "number"}},
        "required": ["name", "category", "reason", "confidence"], "additionalProperties": False}}},
    "required": ["items"],
    "additionalProperties": False,
}


def classify(db, sub: Submission) -> int:
    """Add a prohibited_category value to each commodity. Returns how many were sorted. Raises llm.LLMError;
    the caller logs it and the rules fall back to keywords."""
    names = [f for f in db.query(Field).filter(Field.submission_id == sub.id, Field.entity_type == "commodity",
                                               Field.name == "commodity", Field.status.in_(("OK", "LOW_CONFIDENCE")))
             if f.value.strip()]
    if not names:
        return 0
    distinct = sorted({f.value.strip() for f in names})
    out = llm.structured_call(task="classify_commodities", system=SYSTEM, schema=SCHEMA, max_tokens=4000,
                              content=[{"type": "text", "text": "\n".join(f"- {n}" for n in distinct)}],
                              ctx={"submission_id": sub.id})
    by_name = {i["name"].strip().lower(): i for i in out["items"]}
    count = 0
    for f in names:
        item = by_name.get(f.value.strip().lower())
        if item is None:      # a name the model skipped gets no category: the keyword check applies
            continue
        conf = max(0.0, min(1.0, float(item["confidence"])))
        db.add(Field(submission_id=sub.id, page_id=f.page_id, doc_type=f.doc_type, entity_type="commodity",
                     entity_key=f.entity_key, name=FIELD, value=item["category"], excerpt=item["reason"][:500],
                     method=METHOD, confidence=conf, evidence_verified=False,
                     validation={"sorted_from": f.value, "sorted_from_field": f.id},
                     status="OK" if conf >= REVIEW_THRESHOLD else "LOW_CONFIDENCE"))
        count += 1
    db.flush()
    return count
