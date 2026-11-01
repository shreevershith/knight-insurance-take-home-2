"""Evidence verification and confidence.

A value is only trusted if its excerpt exists in the page text and the value appears in that
excerpt. For scans the "page text" is the vision transcript (model output), which is why vision
values start lower and need cross-checks to clear the review threshold.
"""
import re

from app.extract.normalize import BOOL_FIELDS, normalize

METHOD_PRIOR = {"xlsx": 0.98, "llm_text": 0.90, "llm_vision": 0.75, "human": 1.0}
REVIEW_THRESHOLD = 0.85  # below this the value is LOW_CONFIDENCE and flagged for review


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().upper()


def _alnum(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def excerpt_in_source(excerpt: str, source: str) -> bool:
    if not excerpt or not source:
        return False
    if _squash(excerpt) in _squash(source):
        return True
    # tolerate spacing/punctuation differences in table layouts
    return len(_alnum(excerpt)) >= 4 and _alnum(excerpt) in _alnum(source)


def value_in_excerpt(field: str, value: str, excerpt: str) -> bool | None:
    """None = not checkable (derived values such as checkbox states)."""
    if field in BOOL_FIELDS:
        return None
    v = _alnum(value)
    if not v:
        return None
    if v in _alnum(excerpt):
        return True
    # same value written differently (e.g. 09/14/79 vs 09/14/1979, $1,000,000 vs 1000000)
    nv = normalize(field, value)
    return nv is not None and any(normalize(field, tok) == nv for tok in _tokens(excerpt))


def _tokens(excerpt: str):
    parts = re.split(r"[\s|:]+", excerpt)
    yield from parts
    for i in range(len(parts) - 2):  # 'September 30, 2025'
        yield " ".join(parts[i:i + 3])


def score(method: str, field: str, value: str, excerpt: str, source: str, checks: dict) -> tuple[float, dict]:
    """Confidence = method prior x evidence x validators. Returns (confidence, evidence_checks)."""
    evidence = {"excerpt_in_source": excerpt_in_source(excerpt, source)}
    in_excerpt = value_in_excerpt(field, value, excerpt)
    if in_excerpt is not None:
        evidence["value_in_excerpt"] = in_excerpt
    conf = METHOD_PRIOR.get(method, 0.5)
    if not evidence["excerpt_in_source"]:
        conf *= 0.4
    elif in_excerpt is False:
        conf *= 0.6
    elif in_excerpt is None:
        conf *= 0.95  # derived (checkbox) value: slightly below a literal match
    # A failed validator on a scan suggests a misread; on a text layer it means the source itself
    # is wrong (e.g. a 16-character VIN), which is a rule finding, not low extraction confidence.
    if method == "llm_vision" and any(passed is False for passed in checks.values()):
        conf *= 0.7
    return round(conf, 3), evidence
