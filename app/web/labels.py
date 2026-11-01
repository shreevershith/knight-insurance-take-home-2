"""Readable names for entity keys, so the UI says "Marcus J Whitfield" instead of driver:W38215604720918.

The key stays visible (small, monospace) for traceability; the name is what a reader scans.
"""
from collections import defaultdict
from datetime import datetime

from app.extract.normalize import parse_date


def _title(text: str) -> str:
    return " ".join(w if w.isupper() and len(w) <= 3 else w.capitalize() for w in str(text).split())


def _person(v: dict) -> str:
    if v.get("first_name") or v.get("last_name"):
        return _title(" ".join(x for x in (v.get("first_name"), v.get("middle_initial"), v.get("last_name")) if x))
    name = v.get("full_name") or v.get("driver_name") or ""
    if "," in name:  # "WHITFIELD, MARCUS J" -> "Marcus J Whitfield"
        last, first = name.split(",", 1)
        name = f"{first} {last}"
    return _title(name)


def _quarter(period: str) -> str:
    try:
        d = datetime.strptime(period, "%Y-%m-%d")
        return f"Q{(d.month - 1) // 3 + 1} {d.year}"
    except ValueError:
        return period


def _term(v: dict) -> str:
    start, end = parse_date(v.get("term_start")), parse_date(v.get("term_end"))
    return f"{start:%m/%Y}–{end:%m/%Y}" if start and end else ""


def entity_labels(fields) -> dict[str, str]:
    """entity_key -> readable name, built from the values extracted for that entity."""
    values: dict[str, dict] = defaultdict(dict)
    for f in fields:
        values[f.entity_key].setdefault(f.name, f.value)
    names: dict[str, str] = {}
    for key, v in values.items():
        kind = key.split(":", 1)[0]
        if kind == "applicant":
            names[key] = "Applicant"
        elif kind == "coverage":
            names[key] = "Coverage requested"
        elif kind == "operations":
            names[key] = "Operations"
        elif kind == "driver":
            names[key] = _person(v) or "Driver"
        elif kind == "vehicle":
            vin = v.get("vin") or key.split(":", 1)[1]
            names[key] = " ".join(x for x in (v.get("year"), _title(v.get("make") or "")) if x) + f" · VIN …{vin[-6:]}"
        elif kind == "loss_run":
            line = key.split(":")[1]
            names[key] = f"{line} loss run · {_term(v) or v.get('policy_number', '')}".strip(" ·")
        elif kind == "claim":
            names[key] = f"Claim {v.get('claim_number') or key.split(':', 1)[1]}" + (f" · loss {v['date_of_loss']}" if v.get("date_of_loss") else "")
        elif kind == "prior_policy":
            names[key] = f"Prior policy · {_title(v.get('carrier') or '')} {_term(v)}".strip()
        elif kind == "ifta":
            names[key] = f"IFTA return · {_quarter(key.split(':', 1)[1])}"
        elif kind == "commodity":
            names[key] = f"Commodity · {v.get('commodity') or '?'}"
        elif kind in ("violation", "license_event"):
            what = v.get("description") or v.get("event") or kind.replace("_", " ")
            when = v.get("violation_date") or v.get("date") or ""
            names[key] = f"{_title(what)[:60]}" + (f" · {when}" if when else "")
        elif kind == "sheet_note":
            names[key] = "Spreadsheet note"
    # MVR history items belong to a driver: prefix the driver's name
    for key in list(names):
        if key.startswith(("violation:", "license_event:")):
            driver = ":".join(key.split(":")[1:3])
            if driver in names:
                names[key] = f"{names[driver]} — {names[key]}"
    return names


def label_for(key: str, names: dict[str, str]) -> str:
    """Name for any entity key, including ones with no extracted values (e.g. an unscheduled driver)."""
    if key in names:
        return names[key]
    kind, _, rest = key.partition(":")
    if kind == "driver":
        parts = rest.split("-")  # name-only keys are LAST-FIRST
        return " ".join(p.capitalize() for p in (parts[1:] + parts[:1] if len(parts) == 2 else parts)) + " (not on the driver list)"
    if kind == "vehicle":
        return f"Vehicle · VIN …{rest[-6:]}"
    return key
