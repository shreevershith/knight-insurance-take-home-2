"""Per-page document classification. File names are never used.

Tier 1: content signatures over the text layer (free, explainable).
Tier 2: LLM on the page text when no signature is decisive.
Image-only pages: one vision call that classifies AND transcribes (the transcript then goes
through the same text extraction path as text-layer pages).
"""
import re

from app import config, llm
from app.db import Page
from app.log import get_logger

log = get_logger(__name__)

DOC_TYPES = {
    "APPLICATION": "Insurance application for commercial auto (applicant, coverages, operations questions)",
    "QUOTE_FORM": "Agency quick-quote / submission form",
    "DRIVER_SCHEDULE": "List of drivers (names, DOB, license numbers, hire dates)",
    "VEHICLE_SCHEDULE": "List of vehicles/power units (year, make, VIN, values)",
    "MVR": "Motor vehicle record / driver record report from a state",
    "DRIVER_LICENSE": "Photo or scan of a driver's license card",
    "LOSS_RUN": "Prior-carrier loss history / loss run / no-loss letter",
    "IFTA_RETURN": "IFTA fuel tax return or filing record (jurisdiction miles/gallons)",
    "REFERENCE": "Lookup lists, instructions, or other non-submission reference content",
    "CONTINUATION": "Continuation/end-of-report page belonging to the previous page's document",
    "OTHER": "Anything else",
}

# Signature = (doc_type, required regexes (all), supporting regexes (any adds weight))
_SIGNATURES = [
    ("MVR", [r"driver record"], [r"violations/convictions", r"license and permit information", r"suspensions/revocations"]),
    ("LOSS_RUN", [r"total incurred|paid losses|loss run"], [r"policy number", r"claim", r"insured", r"run date"]),
    ("IFTA_RETURN", [r"ifta|jurisdiction summary"], [r"taxable miles", r"tax paid gal", r"filing period", r"account number"]),
    ("QUOTE_FORM", [r"quick quote form|quote form"], [r"operation type", r"commodities", r"applicant name"]),
    ("APPLICATION", [r"commercial auto insurance application|coverage & rating questions"], [r"fein", r"dot #", r"business name"]),
    ("DRIVER_SCHEDULE", [r"(driver|license).*(dob|date of birth)|(date of birth|dob).*(license)"], [r"first name", r"last name", r"d\.o\.h|hire"]),
    ("VEHICLE_SCHEDULE", [r"\bvin\b"], [r"\byear\b", r"\bmake\b", r"cost new|stated value|value"]),
]
_CONTINUATION = re.compile(r"end of report for", re.I)


def classify_text_rules(text: str) -> tuple[str, float, str] | None:
    lowered = text.lower()
    if _CONTINUATION.search(text) and len(text) < 600:
        return "CONTINUATION", 0.95, "short page containing 'END OF REPORT FOR'"
    best = None
    for doc_type, required, supporting in _SIGNATURES:
        if not all(re.search(r, lowered) for r in required):
            continue
        hits = [r for r in supporting if re.search(r, lowered)]
        score = 0.6 + 0.35 * len(hits) / len(supporting)
        if best is None or score > best[1]:
            best = (doc_type, round(score, 2), f"matched {required + hits}")
    if best and best[1] >= 0.8:
        return best
    return None


_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": list(DOC_TYPES)},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["doc_type", "confidence", "reason"],
    "additionalProperties": False,
}

_VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "readable": {"type": "boolean"},
        "quality_notes": {"type": "string"},
        "doc_type": {"type": "string", "enum": list(DOC_TYPES)},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "transcript": {"type": "string"},
    },
    "required": ["readable", "quality_notes", "doc_type", "confidence", "reason", "transcript"],
    "additionalProperties": False,
}

_TYPES_TEXT = "\n".join(f"- {k}: {v}" for k, v in DOC_TYPES.items())

CLASSIFY_SYSTEM = (
    "You classify single pages from commercial auto insurance submissions sent by insurance agents. "
    "Classify by content only; file names may be wrong. Allowed types:\n" + _TYPES_TEXT +
    "\nconfidence is 0-1. Give a one-sentence reason citing what on the page decided it."
)

VISION_SYSTEM = (
    CLASSIFY_SYSTEM +
    "\n\nThe page is an image (scan or photo). Also produce `transcript`: a faithful plain-text "
    "transcription of every legible word and number on the page, in reading order. Tables: one row "
    "per line, cells separated by ' | '. Copy values exactly as printed (do not normalize dates or "
    "numbers, do not fix typos). Mark anything you cannot read as [illegible]. Never invent text. "
    "If the page is too blurred or damaged to read reliably, set readable=false and explain in quality_notes."
)


def _vision(page: Page, ctx: dict, escalate: bool, reason: str) -> dict:
    return llm.structured_call(
        task="classify_vision", system=VISION_SYSTEM, schema=_VISION_SCHEMA, max_tokens=16000,
        content=[llm.image_block(page.image_path),
                 {"type": "text", "text": "Classify and transcribe this page."}], ctx=ctx,
        escalate=escalate, reason=reason)


def classify_page(page: Page, ctx: dict, escalate: bool = False, reason: str = "") -> None:
    """Sets doc_type/classify_* on the page; image pages also get a transcript. Raises llm.LLMError."""
    if page.has_text_layer:
        hit = classify_text_rules(page.text)
        if hit:
            page.doc_type, page.classify_confidence, page.classify_reason = hit
            page.classify_method = "rules"
            return
        data = llm.structured_call(
            task="classify_text", system=CLASSIFY_SYSTEM, schema=_CLASSIFY_SCHEMA, max_tokens=2000,
            content=[{"type": "text", "text": f"<page>\n{page.text[:20000]}\n</page>"}], ctx=ctx)
        page.doc_type, page.classify_confidence = data["doc_type"], float(data["confidence"])
        page.classify_reason, page.classify_method = data["reason"], "llm_text"
        return

    # A cheap transcription tier gets one re-run on the escalation model if it fails or calls the page
    # unreadable (D-31); IFTA pages are re-run later too if their rows don't add up to the total.
    cheap = escalate is False and config.route("transcribe")[0] != config.ESCALATION_MODEL
    try:
        data = _vision(page, ctx, escalate, reason)
    except llm.LLMError as exc:
        if not cheap:
            raise
        data = _vision(page, ctx, True, f"model error: {exc}")
    else:
        if cheap and not data["readable"]:
            data = _vision(page, ctx, True, "cheap tier marked the page unreadable")
    page.doc_type, page.classify_confidence = data["doc_type"], float(data["confidence"])
    page.classify_reason, page.classify_method = data["reason"], "llm_vision"
    if not data["readable"]:
        page.status = "UNREADABLE"
        page.error = data["quality_notes"]
        return
    page.text, page.text_source = data["transcript"], "vision_transcript"
