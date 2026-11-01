"""The remaining guideline sections: appetite and coverage, new ventures, the rest of submission
completeness, driver history, selective exposures and prior insurance.

Same contract as engine.py: a predicate returns Result(passed, message, field_ids), where passed=None
means an input could not be determined (NOT_EVALUATED, which refers and never passes). Thresholds come
from params and keyword lists from patterns in the rules YAML.
"""
import re
from datetime import date, timedelta

from app.extract.normalize import parse_date, parse_number, state_code
from app.rules import derived
from app.rules.engine import APP_DOCS, Result, _per_driver, commodity_lists, missing, rule

LOSS_DOCS = {"LOSS_RUN"}
SCHEDULE_DOCS = {"VEHICLE_SCHEDULE", "DRIVER_SCHEDULE", "APPLICATION", "QUOTE_FORM"}


# ---------------------------------------------------------------- helpers

def _rx(cfg, name: str):
    return re.compile(cfg["patterns"][name], re.I)


def _months_back(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 - months, 12)
    for day in (d.day, 30, 29, 28):
        try:
            return date(d.year + y, m + 1, day)
        except ValueError:
            continue


def _month_end(year: int, month: int) -> date:
    return (date(year + month // 12, month % 12 + 1, 1)) - timedelta(days=1)


def _anchor(v, cfg):
    return derived.anchor(v, cfg["settings"]["anchor_date_field"])


def _age(dob: date, on: date) -> int:
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def _vehicle_types(v) -> list[tuple[str, str, list[int]]]:
    """(unit label, type text, field ids) per scheduled unit. Type from vehicle_type, else the model or
    body text (the sample's schedule has no type column; its model column says TRACTOR)."""
    out = []
    for key in v.scheduled_vehicles():
        kind = v.value(key, "vehicle_type")
        if not kind.raw:
            kind = v.value(key, "model")
        year, make = v.value(key, "year").raw, v.value(key, "make").raw
        out.append((f"{year or '?'} {make or '?'}", kind.raw or "", kind.field_ids))
    return out


def _has_mvr(v, key) -> bool:
    return any(v.value(key, n, {"MVR"}).raw for n in ("license_status", "mvr_order_date", "license_class"))


def _history(v, key) -> list[tuple[str, date | None, float | None, list[int], list[int]]]:
    """(text, date, points, field ids, date+points field ids) for each MVR violation and license event
    of one driver. The last list is what a points count relies on (the description doesn't matter)."""
    out = []
    for ek in v.entities(f"violation:{key}:"):
        kind, desc = v.value(ek, "event_type"), v.value(ek, "description")
        when, conv, pts = v.value(ek, "violation_date"), v.value(ek, "conviction_date"), v.value(ek, "points")
        text = " ".join(x for x in (kind.raw, desc.raw) if x)
        # cite what a finding rests on: the description and date (the event type is only a label, often
        # inferred from a section heading rather than printed on the row; held-out finding, HO2)
        out.append((text, parse_date(when.raw) or parse_date(conv.raw), parse_number(pts.raw),
                    desc.field_ids + when.field_ids + pts.field_ids, when.field_ids + pts.field_ids))
    for ek in v.entities(f"license_event:{key}:"):
        event, when = v.value(ek, "event"), v.value(ek, "date")
        out.append((event.raw or "", parse_date(when.raw), None, event.field_ids + when.field_ids, when.field_ids))
    return out


def _new_venture(v, cfg) -> tuple[bool | None, str, list[int]]:
    """D-16: new venture = answered yes, or fewer years in business than new_venture_max_years."""
    answer, years = v.value("applicant", "new_venture"), v.value("applicant", "years_in_business")
    ids = answer.field_ids + years.field_ids
    if answer.norm is True:
        return True, "application answers new venture: yes", ids
    n = parse_number(years.raw)
    if n is not None:
        return n < cfg["params"]["new_venture_max_years"], f"{years.raw} years in business", ids
    if answer.norm is False:
        return False, "application answers new venture: no", ids
    return None, "", ids


def _entity_kind(v, cfg):
    """'small' (sole prop / partnership / LLC), 'corporation' or None, from the entity type, else the
    legal-name suffix. The named insured's INC/LLC conflict, if open, already refers on its own."""
    for val in (v.value("applicant", "entity_type"), v.value("applicant", "business_name")):
        if val.raw and _rx(cfg, "small_entity").search(val.raw):
            return "small", val
        if val.raw and _rx(cfg, "corporation").search(val.raw):
            return "corporation", val
    return None, v.value("applicant", "entity_type")


def _terms(v, prefix: str) -> list[tuple[date, date, str, list[int]]]:
    """Policy terms (start, end, label, field ids) for entities under prefix, sorted by start."""
    out = []
    for key in v.entities(prefix):
        start, end = v.value(key, "term_start"), v.value(key, "term_end")
        carrier = v.value(key, "carrier")
        s, e = parse_date(start.raw), parse_date(end.raw)
        if s and e:
            label = f"{carrier.raw + ' ' if carrier.raw else ''}{s:%m/%d/%Y}–{e:%m/%d/%Y}"
            out.append((s, e, label, start.field_ids + end.field_ids))  # the carrier is only a label
    return sorted(out)


def _gaps(terms, start: date, end: date, max_lapse: int) -> list[tuple[date, date, int]]:
    """Uncovered stretches longer than max_lapse days between start and end, given coverage terms."""
    gaps, cursor = [], start
    for s, e, *_ in terms:
        if e < cursor:
            continue
        if s > cursor and (s - cursor).days > max_lapse:
            gaps.append((cursor, min(s, end), (min(s, end) - cursor).days))
        cursor = max(cursor, e)
        if cursor >= end:
            break
    if cursor < end and (end - cursor).days > max_lapse:
        gaps.append((cursor, end, (end - cursor).days))
    return gaps


# ---------------------------------------------------------------- appetite and coverage

@rule("APP-01")
def power_units_are_tractors(v, cfg) -> Result:
    units = _vehicle_types(v)
    if not units:
        return missing(v, "Vehicle schedule", SCHEDULE_DOCS)
    ids = [i for *_, f in units for i in f]
    tractor = _rx(cfg, "tractor")
    other = [f"{u} ({t})" for u, t, _ in units if t and not tractor.search(t)]
    unknown = [u for u, t, _ in units if not t]
    if other:
        return Result(False, "not a tractor: " + "; ".join(other), ids)
    if unknown:
        return Result(None, "vehicle type not stated for: " + ", ".join(unknown), ids)
    return Result(True, f"all {len(units)} scheduled units are tractors", ids)


@rule("APP-02")
def no_ineligible_vehicles(v, cfg) -> Result:
    units = _vehicle_types(v)
    if not units:
        return missing(v, "Vehicle schedule", SCHEDULE_DOCS)
    ineligible, box = _rx(cfg, "ineligible_vehicle"), _rx(cfg, "box_truck")
    hits = [(u, t, f) for u, t, f in units if t and ineligible.search(t) and not box.search(t)]
    if hits:
        return Result(False, "ineligible vehicle type: " + "; ".join(f"{u} ({t})" for u, t, _ in hits),
                      [i for *_, f in hits for i in f])
    ids = [i for *_, f in units for i in f]
    if all(not t for _, t, _ in units):
        return Result(None, "vehicle types not stated on the schedule", ids)
    return Result(True, f"no ineligible types among {len(units)} units", ids)


@rule("APP-03")
def operates_across_state_lines(v, cfg) -> Result:
    states, state_ids = derived.operating_states(v)
    if len(states) > 1:
        return Result(True, f"IFTA miles in {len(states)} jurisdictions", state_ids)
    stated = [v.value("operations", n) for n in ("states_of_operation", "radius_summary")]
    said = next((s for s in stated if s.raw and _rx(cfg, "interstate").search(s.raw)), None)
    if said:
        return Result(True, f"operations stated as '{said.raw}'", said.field_ids)
    if len(states) == 1:
        return Result(False, f"IFTA miles in one jurisdiction only ({next(iter(states))})",
                      state_ids + [i for s in stated for i in s.field_ids])
    return missing(v, "Operating territory (IFTA or stated radius)", {"IFTA_RETURN"} | APP_DOCS)


@rule("COV-01")
def no_al_deductible(v, cfg) -> Result:
    deductible = v.value("coverage", "al_deductible")
    if not deductible.raw:
        return missing(v, "AL deductible answer", APP_DOCS)
    if _rx(cfg, "no_deductible").search(deductible.raw) or parse_number(deductible.raw) == 0:
        return Result(True, f"no AL deductible requested ('{deductible.raw}')", deductible.field_ids)
    return Result(False, f"AL deductible requested: {deductible.raw}. Deductibles are not allowed; quote without one",
                  deductible.field_ids)


@rule("COV-02")
def apd_not_offered(v, cfg) -> Result:
    # D-09: a next action for the agent, not a reason to decline or refer the account (FLAG)
    apd = v.value("coverage", "apd_requested")
    if apd.norm is True:
        return Result(False, "APD quote requested: not available. Quote AL (and cargo) only and tell the agent",
                      apd.field_ids)
    if apd.norm is False:
        return Result(True, "no APD quote requested", apd.field_ids)
    return missing(v, "APD request answer", APP_DOCS)


@rule("NV-01")
def new_venture_small_entity(v, cfg) -> Result:
    new, why, ids = _new_venture(v, cfg)
    if new is None:
        return missing(v, "New-venture answer or years in business", APP_DOCS)
    if not new:
        return Result(True, f"not a new venture ({why})", ids)
    kind, entity = _entity_kind(v, cfg)
    if kind == "corporation":
        return Result(True, "new venture, but a corporation: see NV-02", ids + entity.field_ids)
    if kind is None:
        return Result(None, f"new venture ({why}); entity type not determinable", ids)
    return Result(None, f"new venture {entity.raw} ({why}): needs 2 years documented CDL experience and a "
                        "resume or letter of experience, which an underwriter has to check", ids + entity.field_ids)


@rule("NV-02")
def new_venture_corporation(v, cfg) -> Result:
    new, why, ids = _new_venture(v, cfg)
    if new is None:
        return missing(v, "New-venture answer or years in business", APP_DOCS)
    if not new:
        return Result(True, f"not a new venture ({why})", ids)
    kind, entity = _entity_kind(v, cfg)
    if kind == "corporation":
        return Result(False, f"new venture corporation ({entity.raw}, {why}): subject to underwriter review",
                      ids + entity.field_ids)
    if kind is None:
        return Result(None, f"new venture ({why}); entity type not determinable", ids)
    return Result(True, f"new venture is a {entity.raw}, not a corporation: see NV-01", ids + entity.field_ids)


# ---------------------------------------------------------------- states

@rule("ST-03")
def illinois_selective(v, cfg) -> Result:
    state = v.value("applicant", "mailing_state")
    code = state_code(state.raw)
    if not code:
        return missing(v, "Applicant state", APP_DOCS)
    selective = code in cfg["params"]["selective_states"]
    return Result(not selective, f"domicile {code}" + (" is a selective state: underwriter review" if selective
                                                        else " is not a selective state"), state.field_ids)


# ---------------------------------------------------------------- submission completeness

@rule("SUB-03")
def loss_runs_current(v, cfg) -> Result:
    on, anchor_ids = _anchor(v, cfg)
    if on is None:
        return Result(None, "Application date not found; loss-run age needs it as the anchor date", anchor_ids)
    runs = v.entities("loss_run:")
    if not runs:
        return missing(v, "Loss runs", LOSS_DOCS, required=True)
    max_age = cfg["params"]["loss_run_max_age_days"]
    stale, unknown, ids = [], [], list(anchor_ids)
    for key in runs:
        valued = v.value(key, "run_date")
        ids += valued.field_ids
        d = parse_date(valued.raw)
        name = key.split(":", 1)[1]
        if not d:
            unknown.append(name)
        elif (on - d).days > max_age:
            stale.append(f"{name} valued {valued.raw} ({(on - d).days} days)")
    if stale:
        return Result(False, f"older than {max_age} days: " + "; ".join(stale), ids)
    if unknown:
        return Result(None, "valuation date not found for: " + ", ".join(unknown), ids)
    return Result(True, f"all {len(runs)} loss runs valued within {max_age} days of the application", ids)


@rule("SUB-04")
def loss_run_history(v, cfg) -> Result:
    # D-17: required for AL; cargo (MTC) loss runs are advisory
    on, anchor_ids = _anchor(v, cfg)
    terms = _terms(v, "loss_run:AL:")
    if not terms:
        return missing(v, "Auto liability loss runs", LOSS_DOCS, required=True)
    need = cfg["params"]["loss_run_prior_years"]
    expiring = next((t for t in terms if on and t[0] <= on <= t[1]), terms[-1])
    prior = [t for t in terms if t[0] < expiring[0]]
    ids = anchor_ids + [i for t in terms for i in t[3]]
    summary = f"expiring {expiring[2]} + {len(prior)} prior AL term(s)"
    if len(prior) >= need:
        return Result(True, summary, ids)
    listed = [t for t in _terms(v, "prior_policy:") if t[0] < expiring[0]
              and not any(abs((t[0] - lr[0]).days) <= 3 for lr in terms)]
    gap = ("; no loss run for listed prior term(s): " + ", ".join(t[2] for t in listed)) if listed else ""
    return Result(False, f"{summary}; {need} prior years required{gap}", ids + [i for t in listed for i in t[3]])


@rule("SUB-05")
def ifta_four_quarters(v, cfg) -> Result:
    # D-15: the most recent quarters whose returns were due by the anchor date (due: end of the next month)
    on, anchor_ids = _anchor(v, cfg)
    if on is None:
        return Result(None, "Application date not found; the IFTA window needs it", anchor_ids)
    ends = sorted(_month_end(y, m) for y in range(on.year - 2, on.year + 1) for m in (3, 6, 9, 12)
                  if _month_end(y + m // 12, m % 12 + 1) <= on)[-cfg["params"]["ifta_quarters"]:]
    present = {key.split(":", 1)[1]: key for key in v.entities("ifta:")}
    ids = list(anchor_ids)
    for key in present.values():
        ids += v.value(key, "period_end").field_ids
    label = lambda d: f"Q{(d.month - 1) // 3 + 1} {d.year}"  # noqa: E731
    absent = [label(d) for d in ends if d.isoformat() not in present]
    if absent:
        if v.unread({"IFTA_RETURN"}):
            return Result(None, "not found: " + ", ".join(absent) + " (some IFTA pages could not be read)", ids)
        return Result(False, "missing IFTA return(s): " + ", ".join(absent), ids)
    return Result(True, "returns on file for " + ", ".join(label(d) for d in ends), ids)


@rule("SUB-06")
def driver_schedule_present(v, cfg) -> Result:
    drivers = derived.roster(v)
    if not drivers:
        return missing(v, "Driver schedule", {"DRIVER_SCHEDULE", "VEHICLE_SCHEDULE", "QUOTE_FORM", "APPLICATION"},
                       required=True)
    ids = [i for k in drivers for i in v.value(k, "last_name").field_ids + v.value(k, "full_name").field_ids]
    return Result(True, f"{len(drivers)} scheduled drivers", ids)


@rule("SUB-07")
def mvrs_current(v, cfg) -> Result:
    max_age = cfg["params"]["mvr_max_age_days"]

    def check(key, on):
        ordered = v.value(key, "mvr_order_date", {"MVR"})
        if not ordered.raw:
            if v.unread({"MVR"}):
                return None, "no MVR found (some MVR pages could not be read)", []
            return False, "no MVR on file", []
        d = parse_date(ordered.raw)
        if not d:
            return None, f"MVR date '{ordered.raw}' not readable", ordered.field_ids
        age = (on - d).days
        return age <= max_age, f"MVR ordered {ordered.raw} ({age} days before the application)", ordered.field_ids
    return _per_driver(v, check)


# ---------------------------------------------------------------- drivers

@rule("DRV-01")
def valid_license(v, cfg) -> Result:
    accepted = {s.upper() for s in cfg["params"]["valid_license_statuses"]}

    def check(key, on):
        status, exp = v.value(key, "license_status", {"MVR"}), v.value(key, "license_expiration", {"MVR"})
        ids = status.field_ids + exp.field_ids
        if not status.raw:
            return None, "no MVR license status", ids
        if " ".join(status.raw.upper().split()) not in accepted:
            return False, f"license status {status.raw}", ids
        expires = parse_date(exp.raw)
        if expires and expires < on:
            return False, f"license expired {exp.raw}", ids
        return True, f"license {status.raw}" + (f", expires {exp.raw}" if exp.raw else ""), ids
    return _per_driver(v, check)


@rule("DRV-05")
def dot_medical_65(v, cfg) -> Result:
    med_age = cfg["params"]["dot_medical_age"]

    def check(key, on):
        dob = v.value(key, "dob")
        d = parse_date(dob.raw)
        if not d:
            return None, "DOB not found", dob.field_ids
        age = _age(d, on)
        if age < med_age:
            return True, f"age {age}: no medical report needed", dob.field_ids
        return None, (f"age {age}: a DOT medical examination report is required; it is not a document type this "
                      "system reads, so an underwriter confirms it"), dob.field_ids
    return _per_driver(v, check)


@rule("DRV-06")
def driver_points(v, cfg) -> Result:
    p = cfg["params"]
    max36, max12 = p["points_max_36_months"], p["points_max_12_months"]

    def check(key, on):
        if not _has_mvr(v, key):
            return None, "no MVR to count points from", []
        scored = [h for h in _history(v, key) if h[2]]
        ids = [i for h in scored for i in h[4]]
        if not scored:
            return True, "no points on the MVR", ids
        since36, since12 = _months_back(on, 36), _months_back(on, 12)
        p36 = sum(h[2] for h in scored if h[1] and h[1] >= since36)
        p12 = sum(h[2] for h in scored if h[1] and h[1] >= since12)
        msg = f"{p36:g} points in 36 months, {p12:g} in 12 months"
        if p36 > max36 or p12 > max12:
            return False, msg + f" (limits {max36} / {max12})", ids
        if any(not h[1] for h in scored):
            return None, msg + "; some point entries have no date", ids
        return True, msg, ids
    return _per_driver(v, check)


@rule("DRV-07")
def no_recent_dui(v, cfg) -> Result:
    dui, months = _rx(cfg, "dui"), cfg["params"]["dui_lookback_months"]

    def check(key, on):
        if not _has_mvr(v, key):
            return None, "no MVR to check", []
        hits = [h for h in _history(v, key) if dui.search(h[0])]
        since = _months_back(on, months)
        recent = [h for h in hits if h[1] and h[1] >= since]
        if recent:
            return False, "; ".join(f"{h[0]} on {h[1]:%m/%d/%Y}" for h in recent), [i for h in recent for i in h[3]]
        if any(not h[1] for h in hits):
            return None, "DUI/DWI entry without a date", [i for h in hits for i in h[3]]
        return True, "no DUI/DWI in 5 years" + (f" ({len(hits)} older)" if hits else ""), []
    return _per_driver(v, check)


@rule("DRV-08")
def no_unacceptable_history(v, cfg) -> Result:
    unacceptable, speeding = _rx(cfg, "unacceptable_history"), _rx(cfg, "speeding_over")
    limit = cfg["params"]["speeding_over_limit_mph"]

    def over_limit(text: str) -> bool | None:
        """True: speeding definitely more than `limit` mph over; None: a range that may be; False: neither."""
        m = speeding.search(text)
        if not m:
            return False
        low = int(m.group(1) or m.group(3))
        high = int(m.group(2)) if m.group(2) else None
        if low > limit:
            return True
        return None if high and high > limit else False

    def check(key, on):
        if not _has_mvr(v, key):
            return None, "no MVR to check", []
        bad, maybe = [], []
        for text, when, _, ids, _ in _history(v, key):
            over = over_limit(text)
            if unacceptable.search(text) or over:
                bad.append((text, when, ids))
            elif over is None:
                maybe.append((text, when, ids))
        fmt = lambda h: f"{h[0]}" + (f" on {h[1]:%m/%d/%Y}" if h[1] else "")  # noqa: E731
        if bad:
            return False, "; ".join(fmt(h) for h in bad), [i for h in bad for i in h[2]]
        if maybe:
            return None, "speeding range may exceed the limit: " + "; ".join(fmt(h) for h in maybe), \
                [i for h in maybe for i in h[2]]
        return True, "none on the MVR", []
    return _per_driver(v, check)


# ---------------------------------------------------------------- selective exposures

@rule("SEL-01")
def auto_hauler(v, cfg) -> Result:
    answer = v.value("operations", "auto_hauler")
    if answer.norm is True:
        return Result(False, "auto hauler: answered yes", answer.field_ids)
    pattern = _rx(cfg, "auto_hauler")
    commodities = [v.value(k, "commodity") for k in v.entities("commodity:")]
    commodities = [c for c in commodities if c.raw]
    hits = [c for c in commodities if pattern.search(c.raw)]
    if hits:
        return Result(False, "auto hauler: commodity " + ", ".join(c.raw for c in hits), [i for c in hits for i in c.field_ids])
    ids = answer.field_ids + [i for c in commodities for i in c.field_ids]
    if answer.norm is False:
        return Result(True, "auto hauler: answered no", ids)
    complete = [lst for lst in commodity_lists(v) if lst["complete"]]  # D-29: not listed counts only if complete
    if complete:
        return Result(True, f"no automobiles among the commodities; the list is complete ({complete[0]['total']:g}%)",
                      [i for lst in complete for i in lst["ids"]])
    if commodities:
        return Result(None, "no automobiles listed, but the commodity list is not shown complete", ids)
    return missing(v, "Auto-hauler answer or commodity list", APP_DOCS)


@rule("SEL-02")
def box_trucks(v, cfg) -> Result:
    units = _vehicle_types(v)
    if not units:
        return missing(v, "Vehicle schedule", SCHEDULE_DOCS)
    box = _rx(cfg, "box_truck")
    hits = [(u, t, f) for u, t, f in units if t and box.search(t)]
    if hits:
        return Result(False, "box truck / transit van: " + "; ".join(f"{u} ({t})" for u, t, _ in hits)
                      + f". Minimum premium ${cfg['params']['box_truck_min_premium']:,}", [i for *_, f in hits for i in f])
    ids = [i for *_, f in units for i in f]
    if all(not t for _, t, _ in units):
        return Result(None, "vehicle types not stated on the schedule", ids)
    return Result(True, f"no box trucks or transit vans among {len(units)} units", ids)


@rule("SEL-03")
def low_premium_needs_twenty_units(v, cfg) -> Result:
    p = cfg["params"]
    units = len(v.scheduled_vehicles())
    if units >= p["low_premium_min_units"]:
        return Result(True, f"{units} power units: the ${p['low_premium_per_unit']:,}/unit floor does not apply")
    return Result(None, f"{units} power units, so the account must pay at least ${p['low_premium_per_unit']:,} per unit; "
                        "the submission states no requested premium, so this is checked at quoting")


# ---------------------------------------------------------------- prior insurance

def _coverage_terms(v):
    """Evidence of coverage: terms listed on the application/quote, plus loss-run terms only where they
    add a term the listing doesn't have. A rule cites only what it relies on, so a duplicate read off a
    photo can't hold back a result the typed listing already supports."""
    listed = _terms(v, "prior_policy:")
    extra = [t for t in _terms(v, "loss_run:") if not any(abs((t[0] - lt[0]).days) <= 3 for lt in listed)]
    return sorted(listed + extra)


@rule("PRI-01")
def continuous_coverage(v, cfg) -> Result:
    on, anchor_ids = _anchor(v, cfg)
    if on is None:
        return Result(None, "Application date not found; the 3-year window needs it", anchor_ids)
    terms = _coverage_terms(v)
    if not terms:
        return missing(v, "Prior insurance terms", APP_DOCS | LOSS_DOCS)
    p = cfg["params"]
    start = _months_back(on, 12 * p["prior_coverage_years"])
    gaps = _gaps(terms, start, on, p["max_lapse_days"])
    ids = anchor_ids + [i for t in terms for i in t[3]]
    if gaps:
        return Result(False, "; ".join(f"no coverage {a:%m/%d/%Y}–{b:%m/%d/%Y} ({n} days)" for a, b, n in gaps)
                      + f" within the 3 years before {on:%m/%d/%Y}", ids)
    return Result(True, f"covered from {start:%m/%d/%Y} to {on:%m/%d/%Y} with no lapse over {p['max_lapse_days']} days", ids)


@rule("PRI-02")
def lapses_explained(v, cfg) -> Result:
    terms = _coverage_terms(v)
    if not terms:
        return missing(v, "Prior insurance terms", APP_DOCS | LOSS_DOCS)
    max_lapse = cfg["params"]["max_lapse_days"]
    gaps = _gaps(terms, terms[0][0], max(t[1] for t in terms), max_lapse)
    ids = [i for t in terms for i in t[3]]
    if gaps:
        return Result(False, "; ".join(f"lapse {a:%m/%d/%Y}–{b:%m/%d/%Y} ({n} days)" for a, b, n in gaps)
                      + ": needs a written explanation before quoting", ids)
    return Result(True, f"no lapse over {max_lapse} days between {len(terms)} terms", ids)


@rule("PRI-03")
def losses_for_every_prior_term(v, cfg) -> Result:
    listed = _terms(v, "prior_policy:")
    if not listed:
        return missing(v, "Prior insurance terms on the application", APP_DOCS)
    loss_runs = _terms(v, "loss_run:")
    without = [t for t in listed if not any(abs((t[0] - lr[0]).days) <= 3 for lr in loss_runs)]
    ids = [i for t in listed for i in t[3]]
    if without:
        if v.unread(LOSS_DOCS):
            return Result(None, "no loss run found for " + ", ".join(t[2] for t in without)
                          + " (some loss-run pages could not be read)", ids)
        return Result(False, "no loss run for listed term(s): " + ", ".join(t[2] for t in without), ids)
    return Result(True, f"loss runs on file for all {len(listed)} listed terms", ids)
