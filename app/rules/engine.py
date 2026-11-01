"""Deterministic rules engine. Config (YAML) says what each rule checks and what failing means;
the predicate functions below compute it from the resolved submission view.

Outcomes: PASS, FLAG (informational), REFER, DECLINE, NOT_EVALUATED (input could not be
determined; treated as REFER, never as a pass), ERROR (the rule itself crashed; treated as REFER).
Decision = worst outcome across rules.

Two central guards (applied in evaluate, not per rule):
- DECLINE needs positive evidence: no input fields -> NOT_EVALUATED.
- A result that relies on a LOW_CONFIDENCE extracted value is not trusted: PASS -> NOT_EVALUATED,
  DECLINE -> REFER ("pending review"). The evidence check therefore changes decisions.
"""
import hashlib
import re
import uuid

import yaml

from app.config import RULES_FILE
from app.db import RuleResult, Submission, audit
from app.extract.normalize import parse_number, vin_check
from app.log import get_logger
from app.rules.view import SubmissionView

log = get_logger(__name__)
SEVERITY = {"PASS": 0, "FLAG": 1, "NOT_EVALUATED": 2, "ERROR": 2, "REFER": 2, "DECLINE": 3}
FAIL_OUTCOMES = {"REFER", "DECLINE", "FLAG"}
PREDICATES = {}
REQUIRED_PATTERNS = ("PRO-02", "PRO-06", "PRO-08", "cdl_event", "tractor", "ineligible_vehicle", "box_truck",
                     "interstate", "no_deductible", "dui", "unacceptable_history", "speeding_over",
                     "auto_hauler", "small_entity", "corporation")


def rule(rule_id):
    def wrap(fn):
        PREDICATES[rule_id] = fn
        return fn
    return wrap


class ConfigError(ValueError):
    pass


_last_good: dict | None = None


def validate_config(cfg) -> list[str]:
    errors = []
    if not isinstance(cfg, dict):
        return ["config is not a mapping"]
    for key in ("version", "settings", "params", "rules"):
        if key not in cfg:
            errors.append(f"missing top-level key '{key}'")
    if errors:
        return errors
    if cfg["settings"].get("operating_states_mode") not in ("refer", "decline"):
        errors.append("settings.operating_states_mode must be 'refer' or 'decline'")
    if not cfg["settings"].get("anchor_date_field"):
        errors.append("settings.anchor_date_field is required")
    for name in ("available_states", "small_fleet_max_units", "min_cdl_years", "min_driver_age",
                 "dot_medical_age", "valid_license_statuses"):
        if name not in cfg["params"]:
            errors.append(f"params.{name} is required")
    pats = cfg.get("patterns") or {}
    # every string under patterns is a regex: commodity_prohibited.* plus the top-level ones
    regexes = dict(pats.get("commodity_prohibited") or {})
    regexes.update({k: v for k, v in pats.items() if isinstance(v, str)})
    for name in REQUIRED_PATTERNS:
        if not regexes.get(name):
            errors.append(f"patterns: '{name}' is required")
    for name, pattern in regexes.items():
        try:
            re.compile(pattern or "")
        except re.error as exc:
            errors.append(f"patterns.{name}: invalid regex ({exc})")
    if not isinstance(pats.get("border_states"), list):
        errors.append("patterns.border_states must be a list of state codes")
    seen = set()
    for i, spec in enumerate(cfg["rules"] or []):
        rid = spec.get("id") if isinstance(spec, dict) else None
        if not rid:
            errors.append(f"rules[{i}] has no id")
            continue
        if rid in seen:
            errors.append(f"duplicate rule id {rid}")
        seen.add(rid)
        if spec.get("outcome") not in FAIL_OUTCOMES:
            errors.append(f"{rid}: outcome must be one of {sorted(FAIL_OUTCOMES)}")
        if not spec.get("title"):
            errors.append(f"{rid}: title is required")
    return errors


def load_config() -> dict:
    """Load and validate the rules YAML. An invalid file never takes the engine down: the last good
    config keeps being used and the error is logged (and shown in the audit)."""
    global _last_good
    raw = RULES_FILE.read_bytes()
    try:
        cfg = yaml.safe_load(raw)
        errors = validate_config(cfg)
        if errors:
            raise ConfigError("; ".join(errors))
    except (yaml.YAMLError, ConfigError) as exc:
        if _last_good is None:
            raise ConfigError(f"rules config invalid and no previous good config: {exc}") from exc
        log.error("rules config invalid; using last good config", error=str(exc))
        return {**_last_good, "_config_error": str(exc)}
    cfg["_hash"] = hashlib.sha256(raw).hexdigest()[:10]
    _last_good = cfg
    return cfg


def rules_version(cfg) -> str:
    """Hand-written version plus a hash of the exact YAML, so a live setting flip is a new version."""
    return f"{cfg['version']}+{cfg['_hash']}"


class Result:
    def __init__(self, passed: bool | None, message: str, field_ids=None, outcome: str | None = None):
        self.passed = passed          # None = not evaluable
        self.message = message
        self.field_ids = field_ids or []
        self.outcome = outcome        # overrides the configured failure outcome (e.g. routing)


def missing(v: SubmissionView, what: str, doc_types: set[str], required: bool = False) -> Result:
    """Something was not found.
    - required=True: a document/field the guideline REQUIRES (SUB-*): absence is itself a failure,
      but only if every page that could contain it was actually read (D-22).
    - required=False (default): an answer a rule needs. Absence means unknown -> NOT_EVALUATED,
      never a failure, so a blank checkbox can never DECLINE (D-20)."""
    unread = v.unread(doc_types)
    if unread:
        return Result(None, f"{what} not found, but {len(unread)} page(s) could not be read: "
                            + ", ".join(unread[:4]) + ("..." if len(unread) > 4 else ""))
    if required:
        return Result(False, f"{what} not found")
    return Result(None, f"{what} not found; not determinable")


APP_DOCS = {"APPLICATION", "QUOTE_FORM"}

# ---------------------------------------------------------------- predicates

@rule("SUB-01")
def fein_present(v: SubmissionView, cfg) -> Result:
    fein = v.value("applicant", "fein")
    if fein.raw:
        return Result(True, f"FEIN/SSN on file: {fein.raw}", fein.field_ids)
    return missing(v, "FEIN or SSN", APP_DOCS, required=True)


@rule("SUB-02")
def mc_present(v: SubmissionView, cfg) -> Result:
    mc = v.value("applicant", "mc_number")
    if mc.raw:
        return Result(True, f"MC/docket number: {mc.raw}", mc.field_ids)
    return missing(v, "MC or docket number", APP_DOCS, required=True)


@rule("SUB-08")
def vehicle_schedule_complete(v: SubmissionView, cfg) -> Result:
    units = v.scheduled_vehicles()
    if not units:
        return missing(v, "Vehicle schedule", {"VEHICLE_SCHEDULE", "DRIVER_SCHEDULE", "APPLICATION"}, required=True)
    problems, ids = [], []
    for key in units:
        year, make = v.value(key, "year"), v.value(key, "make")
        vin, stated = v.value(key, "vin"), v.value(key, "stated_value")
        ids += year.field_ids + make.field_ids + vin.field_ids + stated.field_ids
        label = f"{year.raw or '?'} {make.raw or '?'} {vin.raw or key}"
        absent = [n for n, val in (("year", year), ("make", make), ("VIN", vin), ("stated value", stated)) if not val.raw]
        if absent:
            problems.append(f"{label}: missing {', '.join(absent)}")
        if vin.raw:
            checks = vin_check(vin.raw)
            if not checks["length_17"]:
                problems.append(f"{label}: VIN has {len(vin.norm or '')} characters, not 17")
            elif checks.get("check_digit") is False:
                problems.append(f"{label}: VIN check digit fails")
    if problems:
        return Result(False, "; ".join(problems), ids)
    return Result(True, f"{len(units)} units complete", ids)


@rule("CONFLICTS")
def no_open_conflicts(v: SubmissionView, cfg) -> Result:
    if v.open_conflicts:
        names = sorted({f"{c.field_name} ({c.entity_key})" for c in v.open_conflicts})
        return Result(False, f"{len(v.open_conflicts)} unresolved: " + ", ".join(names[:8]) +
                      ("..." if len(names) > 8 else ""))
    return Result(True, "No open conflicts")


@rule("RTE-01")
def route_by_units(v: SubmissionView, cfg) -> Result:
    scheduled = len(v.scheduled_vehicles())
    counts = {"vehicle schedule": scheduled}
    ids = []
    current_units = _current_prior_policy_units(v)
    if current_units:
        counts["prior policy (current term)"] = int(current_units[0])
        ids += current_units[1]
    units = max(counts.values()) if counts else 0
    if not units:  # no power units found anywhere: picking a team would be a guess
        return missing(v, "Power units (vehicle schedule or prior-policy unit count)", {"VEHICLE_SCHEDULE", "QUOTE_FORM", "APPLICATION"})
    team ="Small Fleet" if units <= cfg["params"]["small_fleet_max_units"] else "Fleet"
    detail = ", ".join(f"{k}: {n}" for k, n in counts.items())
    note = " (sources disagree; using the higher count)" if len(set(counts.values())) > 1 else ""
    return Result(True, f"{units} power units -> {team} underwriting [{detail}]{note}", ids, outcome=f"ROUTE:{team}")


from app.rules import derived  # noqa: E402


# ---- drivers (D-06: baseline failures REFER; only the "unacceptable driver history" list DECLINEs)

def _per_driver(v, check) -> Result:
    """Runs check(key, anchor) -> (status, message, ids) per scheduled driver; status True/False/None."""
    on, anchor_ids = derived.anchor(v, v.cfg["settings"]["anchor_date_field"])
    if on is None:
        return Result(None, "Application date not found; driver checks need it as the anchor date", anchor_ids)
    drivers = derived.roster(v)
    if not drivers:
        return missing(v, "Driver schedule", {"DRIVER_SCHEDULE", "VEHICLE_SCHEDULE", "QUOTE_FORM"}, required=True)
    fails, unknown, ok, ids = [], [], [], list(anchor_ids)
    for key in drivers:
        status, msg, fids = check(key, on)
        ids += fids
        label = f"{derived.driver_label(v, key)}: {msg}"
        (ok if status else fails if status is False else unknown).append(label)
    # every driver shown, problems first; " | OK: " separates the drivers that pass (the UI shortens there)
    message = "; ".join(fails + unknown)
    if ok:
        message += (" | OK: " if message else "") + "; ".join(ok)
    if fails:
        return Result(False, message, ids)
    if unknown:
        return Result(None, message, ids)
    return Result(True, message, ids)


@rule("DRV-02")
def valid_cdl(v: SubmissionView, cfg) -> Result:
    def check(key, on):
        cls, flag = v.value(key, "license_class", {"MVR"}), v.value(key, "commercial_license", {"MVR"})
        status, exp = v.value(key, "license_status", {"MVR"}), v.value(key, "license_expiration", {"MVR"})
        ids = cls.field_ids + flag.field_ids + status.field_ids + exp.field_ids
        if not (cls.raw or flag.raw):
            return None, "no MVR license data", ids
        commercial = derived.is_commercial(cls.raw, flag.raw)
        if commercial is False:
            return False, f"no CDL (MVR class {cls.raw or '?'})", ids
        if commercial is None:
            return None, f"CDL status not determinable from class '{cls.raw}'", ids
        if not status.raw:
            return None, "license status not found", ids
        accepted = {s.upper() for s in cfg["params"]["valid_license_statuses"]}
        if " ".join(status.raw.upper().split()) not in accepted:  # exact match: 'INVALID' is not 'VALID'
            return False, f"license status {status.raw}", ids
        exp_date = derived.parse_date(exp.raw)
        if exp_date and exp_date < on:
            return False, f"CDL expired {exp.raw}", ids
        return True, f"valid CDL class {cls.raw or 'A/B'}", ids
    return _per_driver(v, check)


@rule("DRV-03")
def cdl_experience(v: SubmissionView, cfg) -> Result:
    min_years = cfg["params"]["min_cdl_years"]

    def check(key, on):
        commercial = derived.is_commercial(v.value(key, "license_class", {"MVR"}).raw,
                                           v.value(key, "commercial_license", {"MVR"}).raw)
        if commercial is False:
            return False, "no CDL (see DRV-02)", []
        evidence = derived.cdl_evidence(v, key, cfg["patterns"]["cdl_event"])
        if not evidence:
            return None, "no documented CDL date; experience not determinable", []
        first = evidence[0]
        years = derived.years_between(first.on, on)
        msg = f"CDL held since at least {first.on:%m/%d/%Y} ({first.what}) = {years} yrs"
        return years >= min_years, msg, [i for e in evidence for i in e.field_ids]
    return _per_driver(v, check)


@rule("DRV-04")
def driver_age(v: SubmissionView, cfg) -> Result:
    min_age = cfg["params"]["min_driver_age"]  # 65+ medical report: DRV-05

    def check(key, on):
        dob = v.value(key, "dob")
        d = derived.parse_date(dob.raw)
        if not d:
            return None, "DOB not found", dob.field_ids
        age = on.year - d.year - ((on.month, on.day) < (d.month, d.day))
        if age < min_age:
            return False, f"age {age} (< {min_age})", dob.field_ids
        return True, f"age {age}", dob.field_ids
    return _per_driver(v, check)


# ---- states (D-05, D-28)

@rule("ST-01")
def domicile_listed(v: SubmissionView, cfg) -> Result:
    code, where, ids = derived.domicile_state(v)
    if not code:
        return missing(v, "Applicant state", APP_DOCS)
    listed = code in cfg["params"]["available_states"]
    return Result(listed, f"domicile {code} (from {where}) {'is' if listed else 'is NOT'} an available state", ids)


@rule("ST-02")
def texas_north_of_i10(v: SubmissionView, cfg) -> Result:
    domicile = derived.domicile_state(v)[0]
    states, ids = derived.operating_states(v)
    farthest = v.value("operations", "farthest_state")
    touches_tx = domicile == "TX" or "TX" in states or derived.state_code(farthest.raw) == "TX"
    if not touches_tx:
        if not states:  # no IFTA jurisdictions at all: "no Texas" would be a pass on absence of data
            return missing(v, "IFTA jurisdictions (operating states)", {"IFTA_RETURN"})
        return Result(True, f"no Texas miles in {len(states)} IFTA jurisdictions"
                            + (f"; farthest territory {farthest.raw}" if farthest.raw else ""), ids + farthest.field_ids)
    return Result(None, "Texas operations (IFTA miles / farthest territory TX); whether they stay north of "
                        "I-10 is not determinable from the submission (AMB-15)", ids + farthest.field_ids)


@rule("ST-04")
def operating_states_listed(v: SubmissionView, cfg) -> Result:
    states, ids = derived.operating_states(v)
    if not states:
        return missing(v, "IFTA jurisdictions", {"IFTA_RETURN"})
    outside = sorted(states - set(cfg["params"]["available_states"]))
    mode = cfg["settings"]["operating_states_mode"]
    if not outside:
        return Result(True, f"all {len(states)} operating states are available states", ids)
    outcome = "DECLINE" if mode == "decline" else "REFER"
    return Result(False, f"{len(outside)} of {len(states)} IFTA states are not available states "
                         f"({', '.join(outside)}); operating_states_mode={mode}", ids, outcome=outcome)


# ---- prohibited exposures (D-29)

def _answered_no(v, field_name, label) -> Result:
    val = v.value("operations", field_name)
    if val.norm is True:
        return Result(False, f"{label}: answered Yes", val.field_ids)
    if val.norm is False:
        return Result(True, f"{label}: answered No", val.field_ids)
    return missing(v, f"{label} answer", APP_DOCS)


def commodity_lists(v: SubmissionView) -> list[dict]:
    """Commodity lists per source document: names, the total of their percentages, and whether the
    list is provably complete (shares add up to 100%; the app writes 0.5, the quote writes 50)."""
    lists: dict[str, dict] = {}
    for key in v.entities("commodity:"):
        name, pct = v.value(key, "commodity"), v.value(key, "percent")
        if not name.raw:
            continue
        doc = key.split(":")[1]
        lst = lists.setdefault(doc, {"names": [], "shares": [], "ids": []})
        lst["names"].append(name.raw)
        lst["shares"].append(parse_number(pct.raw))
        lst["ids"] += name.field_ids + pct.field_ids
    for lst in lists.values():
        shares = lst["shares"]
        if any(s is None for s in shares):
            lst["total"], lst["complete"] = None, False
            continue
        total = sum(shares) * (100 if all(s <= 1 for s in shares) else 1)
        lst["total"], lst["complete"] = round(total, 1), 98 <= total <= 102
    return list(lists.values())


def _commodity_check(v, pattern, label) -> Result:
    """D-29: 'not listed' is evidence only when the list is complete (shares total 100%), and the
    PASS cites the names and percentages that show it. Otherwise the answer is not determinable."""
    lists = commodity_lists(v)
    if not lists:
        return missing(v, "Commodity list", APP_DOCS)
    hits = [n for lst in lists for n in lst["names"] if re.search(pattern, n, re.I)]
    if hits:
        return Result(False, f"{label}: commodity {', '.join(hits)}",
                      [i for lst in lists for i in lst["ids"]])
    complete = [lst for lst in lists if lst["complete"]]
    if not complete:
        totals = ", ".join(f"{lst['total']}%" if lst["total"] is not None else "shares missing" for lst in lists)
        return Result(None, f"no {label.lower()} listed, but no commodity list is shown complete ({totals})",
                      [i for lst in lists for i in lst["ids"]])
    return Result(True, f"no {label.lower()} among the commodities; the list is complete ("
                  + "; ".join(f"{len(lst['names'])} commodities totalling {lst['total']:g}%" for lst in complete) + ")",
                  [i for lst in complete for i in lst["ids"]])


@rule("PRO-01")
def no_hazmat(v, cfg):
    return _answered_no(v, "hazmat", "Hazardous materials")


@rule("PRO-02")
def no_lithium(v, cfg):
    return _commodity_check(v, cfg["patterns"]["commodity_prohibited"]["PRO-02"], "Lithium battery cargo")


@rule("PRO-03")
def border_proximity(v, cfg):
    cross = v.value("operations", "cross_border")
    states, ids = derived.operating_states(v)
    border = sorted(states & set(cfg["patterns"]["border_states"]))
    answer = f"cross-border answered {cross.raw}" if cross.raw else "cross-border not answered"
    return Result(None, f"Within 50 miles of the US/Mexico border is not determinable ({answer}, which is a "
                        f"different question; border-state miles: {', '.join(border) or 'none found'})",
                  cross.field_ids + ids)


@rule("PRO-05")
def safer(v, cfg):
    dot = v.value("applicant", "dot_number")
    return Result(None, f"SAFER violations need an FMCSA lookup for DOT {dot.raw or '?'} (designed, not built)",
                  dot.field_ids)


@rule("PRO-06")
def no_towing(v, cfg):
    towing = v.value("operations", "towing")
    if towing.norm is True:
        return Result(False, "Towing/recovery: answered Yes", towing.field_ids)
    res = _commodity_check(v, cfg["patterns"]["commodity_prohibited"]["PRO-06"], "Towing/recovery")
    if res.passed is False:   # a towing commodity outweighs a 'No' answer
        return res
    if towing.norm is False:  # an explicit No decides it, as for hazmat and intermodal (held-out #2, HO4)
        return Result(True, "Towing/recovery: answered No", towing.field_ids)
    return res


@rule("PRO-07")
def no_intermodal(v, cfg):
    return _answered_no(v, "intermodal", "Intermodal containers")


@rule("PRO-08")
def no_waste(v, cfg):
    return _commodity_check(v, cfg["patterns"]["commodity_prohibited"]["PRO-08"], "Waste disposal")


def _current_prior_policy_units(v: SubmissionView):
    latest = None
    for key in v.entities("prior_policy:"):
        start = v.value(key, "term_start").norm
        units = v.value(key, "units")
        if units.norm and (latest is None or (start or "") > latest[0]):
            latest = (start or "", units.norm, units.field_ids)
    return (latest[1], latest[2]) if latest else None


# ---------------------------------------------------------------- engine

def _apply_guards(outcome: str, res: Result, view: SubmissionView) -> tuple[str, str]:
    """Central guards: DECLINE needs evidence; nothing PASSes or DECLINEs on a LOW_CONFIDENCE value."""
    message = res.message
    if outcome == "DECLINE" and not res.field_ids:
        return "NOT_EVALUATED", message + " [guard: a decline needs positive evidence; none cited]"
    low = [fid for fid in res.field_ids if (f := view.field_by_id.get(fid)) is not None
           and f.method != "human" and f.status == "LOW_CONFIDENCE"]
    if low and outcome in ("PASS", "DECLINE"):
        refs = ", ".join(f"F{i}" for i in low[:6])
        if outcome == "PASS":
            return "NOT_EVALUATED", message + f" [guard: relies on low-confidence value(s) {refs}; needs review]"
        return "REFER", message + f" [guard: decline pending review of low-confidence value(s) {refs}]"
    return outcome, message


def evaluate(db, sub: Submission, actor: str = "system") -> str:
    cfg = load_config()
    version = rules_version(cfg)
    view = SubmissionView(db, sub.id)
    view.cfg = cfg
    run_id = uuid.uuid4().hex[:12]
    worst, team = "PASS", ""
    for spec in cfg["rules"]:
        fn = PREDICATES.get(spec["id"])
        if fn is None:
            outcome, res = "NOT_EVALUATED", Result(None, "rule defined in config but no predicate implemented")
        else:
            try:
                res = fn(view, cfg)
            except Exception as exc:  # one broken rule must never take down the evaluation
                log.exception("rule crashed", rule_id=spec["id"], submission_id=sub.id)
                res = Result(None, f"rule error: {type(exc).__name__}: {exc}")
                outcome = "ERROR"
            else:
                if res.outcome and res.outcome.startswith("ROUTE:"):
                    team, outcome = res.outcome.split(":", 1)[1], "FLAG"
                elif res.passed is None:
                    outcome = "NOT_EVALUATED"
                elif res.passed:
                    outcome = "PASS"
                else:
                    outcome = res.outcome or spec["outcome"]  # a rule may pick its outcome from config (ST-04)
                outcome, res.message = _apply_guards(outcome, res, view)
        db.add(RuleResult(submission_id=sub.id, run_id=run_id, rule_id=spec["id"], rules_version=version,
                          title=spec["title"], source_clause=spec.get("clause", ""), outcome=outcome,
                          message=res.message, input_field_ids=res.field_ids))
        if SEVERITY[outcome] > SEVERITY[worst]:
            worst = outcome
    decision = {"PASS": "READY_TO_QUOTE", "FLAG": "READY_TO_QUOTE", "NOT_EVALUATED": "REFER", "ERROR": "REFER",
                "REFER": "REFER", "DECLINE": "DECLINE"}[worst]
    before = {"decision": sub.decision, "team": sub.team, "rules_version": sub.rules_version}
    sub.decision, sub.team, sub.rules_version = decision, team, version
    after = {"decision": decision, "team": team, "rules_version": version, "settings": cfg.get("settings", {})}
    if cfg.get("_config_error"):
        after["config_error"] = cfg["_config_error"]
    audit(db, sub.id, actor, "rules.evaluated", f"run:{run_id}", before=before, after=after)
    return run_id


# the remaining guideline sections register their predicates on import
from app.rules import guideline_rules  # noqa: E402,F401
