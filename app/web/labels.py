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


# ---------------------------------------------------------------- plain-language labels (D-45)
# What the page shows instead of internal names: field names, document types, how a value was read, what a check
# found, audit actions and file errors. One place, so every part of the page says the same thing.

FIELD_LABELS = {
    "dob": "date of birth", "driver_dob": "driver's date of birth", "um_limit": "UM limit", "uim_limit": "UIM limit",
    "al_limit": "AL limit", "al_deductible": "AL deductible", "apd_requested": "APD requested",
    "reefer_breakdown": "reefer breakdown cover", "fein": "FEIN", "ssn": "SSN", "dot_number": "DOT number",
    "mc_number": "MC number", "vin": "VIN", "cdl_issue_date": "CDL issue date", "mvr_order_date": "MVR order date",
    "ifta_account": "IFTA account", "scheduled": "not on the schedule", "loss_run": "missing loss run",
    "event": "MVR history event", "date": "event date", "radius_summary": "radius",
}
DOC_TYPE_LABELS = {
    "APPLICATION": "application", "QUOTE_FORM": "quote form", "DRIVER_SCHEDULE": "driver list",
    "VEHICLE_SCHEDULE": "vehicle list", "MVR": "MVR", "DRIVER_LICENSE": "license photo", "LOSS_RUN": "loss run",
    "IFTA_RETURN": "IFTA return", "REFERENCE": "reference sheet", "CONTINUATION": "continuation page",
    "OTHER": "other (e.g. a cover note)",
}
METHOD_LABELS = {
    "llm_text": "read by AI from the document text", "llm_vision": "read by AI from a scan or photo",
    "xlsx": "read from the spreadsheet", "human": "your correction",
}
# check name -> (text when it passed, text when it failed)
CHECK_TEXT = {
    "excerpt_in_source": ("The quoted line is on the page", "The quoted line wasn't found on the page"),
    "value_in_excerpt": ("The quoted line contains the value",
                         "The quoted line doesn't show the whole value; it may be combined from several lines, "
                         "so check it against the page"),
    "length_17": ("VIN has 17 characters", "VIN doesn't have 17 characters"),
    "no_IOQ": ("VIN has no I, O or Q", "VIN contains I, O or Q, which VINs never use"),
    "check_digit": ("VIN check digit is right", "VIN check digit is wrong: likely a typo or misread"),
    "date_parses": ("Reads as a date", "Doesn't read as a date"),
    "dob_plausible": ("Date of birth is plausible", "Date of birth isn't plausible"),
    "year_plausible": ("Model year is plausible", "Model year isn't plausible"),
    "number_parses": ("Reads as an amount", "Doesn't read as an amount"),
    "confirmed_by_other_document": ("Another document says the same", ""),
    "confirmed_by_prior_insurance_listing": ("Matches the prior-insurance listing on the application", ""),
    "superseded_by_human": ("Replaced by your correction", ""),
    "superseded_by_newer_mvr": ("Replaced by a newer MVR for the same driver", ""),
    "ifta_rows_sum_to_total": ("The IFTA rows add up to the printed total",
                               "The IFTA rows don't add up to the printed total"),
}
ACTION_LABELS = {
    "submission.received": "Submission received", "submission.reprocess": "Reprocessed",
    "rules.evaluated": "Rules applied", "field.override": "Value corrected", "conflict.resolve": "Conflict resolved",
    "analysis.rerun": "AI summary regenerated",
}
STATUS_LABELS = {
    "OK": "read", "SKIPPED": "not needed", "CONTAINER": "unpacked", "DUPLICATE": "duplicate",
    "UNREADABLE": "can't be read", "UNSUPPORTED": "file type not read", "MODEL_ERROR": "AI couldn't read it",
    "UNCLASSIFIED": "type unclear", "PENDING": "waiting",
}


def field_label(name: str) -> str:
    return FIELD_LABELS.get(name, (name or "").replace("_", " "))


def doc_type_label(doc_type: str) -> str:
    return DOC_TYPE_LABELS.get(doc_type, (doc_type or "").replace("_", " ").lower())


def method_label(method: str) -> str:
    return METHOD_LABELS.get(method, method or "")


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, (status or "").replace("_", " ").lower())


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, (action or "").replace(".", " ").replace("_", " "))


def check_lines(validation: dict | None) -> list[tuple[bool, str]]:
    """(passed, sentence) for each recorded check, failures first; internal bookkeeping keys are skipped."""
    lines = []
    for key, passed in (validation or {}).items():
        if passed is True or passed is False:
            text = CHECK_TEXT.get(key, (key.replace("_", " "), key.replace("_", " ")))[0 if passed else 1]
            if text:
                lines.append((passed, text))
    return sorted(lines, key=lambda line: line[0])


def plain_error(error: str) -> str:
    """A file error a reviewer can act on; the technical text stays available as a tooltip."""
    e = error or ""
    if "Pdfium" in e or "PDFium" in e:
        return "The PDF is damaged or isn't a real PDF, so it can't be opened: ask the agent to resend it"
    if "BadZip" in e or "bad zip" in e:
        return "The zip file is damaged and can't be opened: ask the agent to resend it"
    if e.startswith("unsupported file type"):
        return e.replace("unsupported file type", "This file type isn't read:") + " (ask for a PDF if it matters)"
    return e
