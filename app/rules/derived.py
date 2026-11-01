"""Facts the rules need that no single document states: CDL experience, operating states, TIV.
Each derived fact carries the field ids it was computed from."""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from app.extract.normalize import parse_bool, parse_date, parse_number, state_code
from app.rules.view import SubmissionView

ROSTER_TYPES = {"DRIVER_SCHEDULE", "VEHICLE_SCHEDULE", "QUOTE_FORM", "APPLICATION"}


@dataclass
class Evidence:
    on: date
    what: str
    field_ids: list[int] = field(default_factory=list)


def years_between(start: date, end: date) -> float:
    return round((end - start).days / 365.25, 1)


def roster(v: SubmissionView) -> list[str]:
    """Drivers the agent scheduled (not drivers only mentioned in a loss run)."""
    return v.entities("driver:", ROSTER_TYPES)


def driver_label(v: SubmissionView, key: str) -> str:
    full = v.value(key, "full_name").raw
    if full:
        return full
    return " ".join(x for x in (v.value(key, "first_name").raw, v.value(key, "last_name").raw) if x) or key


def is_commercial(license_class, commercial_flag) -> bool | None:
    flag = parse_bool(commercial_flag)
    if flag is not None:
        return flag
    if license_class:
        cls = str(license_class).upper()
        if "COMMERCIAL" in cls and "NON" not in cls:
            return True
        letter = re.search(r"\b([ABC])\b", cls)
        if "NON-COMMERCIAL" in cls or "NON COMMERCIAL" in cls:
            return False
        if letter:
            return letter.group(1) in ("A", "B")
    return None


def cdl_evidence(v: SubmissionView, key: str, cdl_event: str | None = None) -> list[Evidence]:
    """Every document that proves a CDL was held on a date (D-26). The earliest is a lower bound on
    CDL experience. A card's issue date proves a CDL from that date (not that it started then);
    'first licensed' covers any license, so it is not used. cdl_event is the rules YAML pattern
    (patterns.cdl_event) for MVR history events that name a CDL; None loads it from the config."""
    if cdl_event is None:
        from app.rules.engine import load_config  # engine imports this module
        cdl_event = load_config()["patterns"]["cdl_event"]
    event_re = re.compile(cdl_event, re.I)
    out: list[Evidence] = []
    per_page = defaultdict(dict)
    for f in v.by_entity.get(key, []):
        per_page[f.page_id][f.name] = f
    for page_fields in per_page.values():
        issued = page_fields.get("license_issue_date")
        cls, flag = page_fields.get("license_class"), page_fields.get("commercial_license")
        if issued and is_commercial(cls.value if cls else None, flag.value if flag else None):
            d = parse_date(issued.value)
            if d:
                ids = [issued.id] + [x.id for x in (cls, flag) if x]
                out.append(Evidence(d, f"commercial license issued {issued.value} ({issued.doc_type})", ids))
        cdl_date = page_fields.get("cdl_issue_date")
        if cdl_date and (d := parse_date(cdl_date.value)):
            out.append(Evidence(d, f"CDL issue date {cdl_date.value} ({cdl_date.doc_type})", [cdl_date.id]))
    for ev_key in v.entities(f"license_event:{key}:"):
        when, what = v.value(ev_key, "date"), v.value(ev_key, "event")
        if what.raw and event_re.search(what.raw) and (d := parse_date(when.raw)):
            out.append(Evidence(d, f"MVR history: {what.raw} {when.raw}", when.field_ids + what.field_ids))
    return sorted(out, key=lambda e: e.on)


_STATE_ZIP = re.compile(r",\s*([A-Za-z]{2})\.?\s+\d{5}(?:-\d{4})?\b")


def domicile_state(v: SubmissionView) -> tuple[str | None, str, list[int]]:
    """(state code, where it came from, field ids). The mailing state if the model filled it; otherwise
    the state inside a one-line address ('418 Industrial Pkwy, Jackson, TN 38301'), read in code and
    checked against the US state list (held-out #2 finding, HO4)."""
    state = v.value("applicant", "mailing_state")
    if state.raw and state_code(state.raw):
        return state_code(state.raw), "mailing state", state.field_ids
    for name in ("mailing_address", "garaging_address"):
        addr = v.value("applicant", name)
        m = _STATE_ZIP.search(addr.raw or "")
        if m and state_code(m.group(1)):
            return state_code(m.group(1)), f"{name.replace('_', ' ')} '{addr.raw}'", addr.field_ids
    return None, "", state.field_ids


def operating_states(v: SubmissionView) -> tuple[set[str], list[int]]:
    """State codes with IFTA miles, deduplicated (Kentucky + Kentucky surcharge = KY)."""
    states, ids = set(), []
    for key in v.entities("ifta_row:"):
        j = v.value(key, "jurisdiction")
        miles = v.value(key, "total_miles")
        code = state_code(j.raw)
        if code and (miles.norm is None or miles.norm > 0):
            states.add(code)
            ids += j.field_ids
    return states, ids


def computed_tiv(v: SubmissionView) -> dict:
    """Sum of cost new / stated value over scheduled units, computed in code (D-27): the vehicle
    list's =SUM cell has no saved value, so it is never read as a number."""
    cost, stated, ids, missing_stated = 0.0, 0.0, [], 0
    for key in v.scheduled_vehicles():
        c, s = v.value(key, "cost_new"), v.value(key, "stated_value")
        cost += c.norm or 0
        if s.norm:
            stated += s.norm
        else:
            missing_stated += 1
        ids += c.field_ids + s.field_ids
    return {"cost_new_total": cost, "stated_value_total": stated or None,
            "units_missing_stated_value": missing_stated, "field_ids": ids, "method": "computed"}


def anchor(v: SubmissionView, field_name: str) -> tuple[date | None, list[int]]:
    val = v.anchor_date(field_name)
    return parse_date(val.raw), val.field_ids


def money(v: SubmissionView, key: str, name: str):
    return parse_number(v.value(key, name).raw)
