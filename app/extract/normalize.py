"""Normalization and validators. Raw values stay as printed; these give comparable forms."""
import re
from datetime import date, datetime

_VIN_TRANSLIT = dict(zip("ABCDEFGHJKLMNPRSTUVWXYZ", [1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 7, 9, 2, 3, 4, 5, 6, 7, 8, 9]))
_VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
_DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S",
                 "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%m/%d/%Y."]
_MONTH_YEAR_RE = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})")
YES = {"yes", "y", "true", "x", "checked", "1"}
NO = {"no", "n", "false", "none", "0", "unchecked"}

DATE_FIELDS = {"dob", "license_issue_date", "license_expiration", "cdl_issue_date", "hire_date", "mvr_order_date",
               "violation_date", "conviction_date", "date", "term_start", "term_end", "run_date",
               "date_of_loss", "date_reported", "driver_dob", "period_begin", "period_end", "due_date",
               "application_date", "effective_date"}
MONEY_FIELDS = {"al_limit", "cargo_limit", "trailer_interchange_limit", "cost_new", "stated_value", "total_paid",
                "total_reserved", "total_incurred", "deductible", "paid", "reserve", "incurred", "subtotal_due",
                "penalty", "interest", "total_due", "total", "average_value", "max_value", "apd_deductible"}
INT_FIELDS = {"year", "units", "total_claims", "open_claims", "points", "total_miles", "taxable_miles",
              "tax_paid_gallons", "total_taxable_gallons", "gvw", "first_licensed_year"}
BOOL_FIELDS = {"apd_requested", "cargo_requested", "reefer_breakdown", "hazmat", "intermodal", "cross_border",
               "doubles_triples", "oversize", "team_driving", "freight_broker", "workers_comp", "sub_haulers",
               "unlisted_vehicles", "towing", "auto_hauler", "new_venture", "owner_is_driver", "signed",
               "commercial_license", "mvr_marked_old", "late_reported", "unit_reported"}
NAME_FIELDS = {"business_name", "full_name", "insured_name", "taxpayer_name", "carrier", "agency_name", "driver_name"}


US_STATES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA", "COLORADO": "CO",
    "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
}
_STATE_CODES = set(US_STATES.values())
STATE_FIELDS = {"license_state", "mailing_state", "registered_state", "farthest_state", "jurisdiction"}


def state_code(value) -> str | None:
    """'California' / 'CA' / 'ca.' -> 'CA'. Unknown (e.g. a Canadian province) -> None, never a guess."""
    text = " ".join(re.sub(r"[^A-Za-z ]", " ", str(value or "")).upper().split())
    if text in _STATE_CODES:
        return text
    return US_STATES.get(text)


def parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().rstrip(".")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    match = _MONTH_YEAR_RE.search(text)
    if match:
        m, d, y = (int(g) for g in match.groups())
        if y < 100:
            y += 2000 if y < 50 else 1900
        try:
            return date(y, m, d)
        except ValueError:
            return None
    return None


def parse_number(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    negative = text.startswith("-") or text.startswith("(") or "-$" in text
    digits = re.sub(r"[^\d.]", "", text)
    if not digits or digits == ".":
        return None
    try:
        number = float(digits)
    except ValueError:
        return None
    return -number if negative else number


def parse_bool(value) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in YES:
        return True
    if text in NO or text.startswith("do not"):
        return False
    return None


def norm_name(value) -> str:
    text = re.sub(r"[^A-Z0-9 ]", " ", str(value or "").upper())
    return " ".join(text.split())


def name_tokens(value) -> set[str]:
    return set(norm_name(value).split())


def names_compatible(a, b) -> bool:
    """'WHITFIELD, MARCUS J' ~ 'MARCUS J WHITFIELD'; 'ALEKSANDR P VOLKOV' ~ 'ALEKSANDR PAVEL VOLKOV'."""
    ta, tb = name_tokens(a), name_tokens(b)
    if ta == tb:
        return True
    long_a = {t for t in ta if len(t) > 1}
    long_b = {t for t in tb if len(t) > 1}
    if not (long_a & long_b):
        return False
    # One side omits a middle name/initial ('Nathaniel Osei' vs 'OSEI, NATHANIEL K'): compatible when
    # every full word of the shorter name appears in the longer one. A DIFFERENT word (INC vs LLC,
    # SERVICES vs AGENCY) still fails below.
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    short_long = {t for t in shorter if len(t) > 1}
    if len(short_long) >= 2 and short_long <= longer and all(
            any(u.startswith(t) for u in longer) for t in shorter - longer):
        return True
    rest_a, rest_b = ta - tb, tb - ta
    # remaining tokens must be initial/full-name pairs
    for t in rest_a:
        if not any(u.startswith(t) or t.startswith(u) for u in rest_b):
            return False
    for t in rest_b:
        if not any(u.startswith(t) or t.startswith(u) for u in rest_a):
            return False
    return True


def coverage_line(value) -> str | None:
    """Line of business as printed -> AL / MTC / APD ('COMMERCIAL AUTO LIABILITY', 'AUTO LIABILITY (AL)',
    'Motor Truck Cargo', 'Physical Damage'). Unrecognized text -> None, never a guess."""
    text = str(value or "").upper()
    if re.search(r"\bMTC\b|CARGO", text):
        return "MTC"
    if re.search(r"\bAPD\b|PHYSICAL DAMAGE|\bPHD\b|\bPD\b", text):
        return "APD"
    if re.search(r"\bAL\b|AUTO\s*LIAB|^\s*LIAB", text):  # 'General Liability' is not auto liability
        return "AL"
    return None


def norm_id(value) -> str:
    """License numbers, VINs, DOT/MC: uppercase alphanumerics only."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def vin_check(vin: str) -> dict:
    v = norm_id(vin)
    result = {"length_17": len(v) == 17, "no_IOQ": not any(c in v for c in "IOQ")}
    if len(v) == 17 and result["no_IOQ"]:
        total = 0
        for ch, weight in zip(v, _VIN_WEIGHTS):
            total += (int(ch) if ch.isdigit() else _VIN_TRANSLIT.get(ch, 0)) * weight
        remainder = total % 11
        result["check_digit"] = (str(remainder) if remainder < 10 else "X") == v[8]
    return result


def looks_like_vin(value) -> bool:
    v = norm_id(value)
    return len(v) >= 11 and any(c.isdigit() for c in v) and any(c.isalpha() for c in v)


def normalize(field: str, value):
    """Comparable form of a raw value, or None if it can't be parsed."""
    if value is None or str(value).strip() == "":
        return None
    if field in DATE_FIELDS:
        d = parse_date(value)
        return d.isoformat() if d else None
    if field in BOOL_FIELDS:
        return parse_bool(value)
    if field in MONEY_FIELDS or field in INT_FIELDS:
        return parse_number(value)
    if field in ("vin", "license_number", "dot_number", "mc_number", "fein", "policy_number", "account_number"):
        return norm_id(value)
    if field in NAME_FIELDS:
        return norm_name(value)
    if field in STATE_FIELDS:
        return state_code(value) or norm_name(value)
    if field == "license_class":  # 'A', 'CLASS A', 'A COMB VEH>26,000 GVWR' -> 'A'
        m = re.search(r"\b(?:CLASS\s+)?([ABCDM])\b", str(value).upper())
        return m.group(1) if m else norm_name(value)
    return " ".join(str(value).upper().split())


def validate(field: str, value) -> dict:
    """Field-level checks. Returns {check_name: passed}. Empty dict = no checks apply."""
    if value is None or str(value).strip() == "":
        return {}
    checks = {}
    if field == "vin":
        checks.update(vin_check(value))
    if field in DATE_FIELDS and field != "hire_date":
        checks["date_parses"] = parse_date(value) is not None
    if field in ("dob", "driver_dob"):
        d = parse_date(value)
        checks["dob_plausible"] = bool(d and 1930 <= d.year <= date.today().year - 16)
    if field == "year":
        n = parse_number(value)
        checks["year_plausible"] = bool(n and 1980 <= n <= date.today().year + 1)
    if field in MONEY_FIELDS:
        checks["number_parses"] = parse_number(value) is not None or str(value).strip() in ("$ -", "-", "$-")
    return checks
