"""Conflict and duplicate detection. Conflicts are surfaced with every source value and a
suggested value, and stay OPEN until a human resolves them. Nothing is silently picked."""
import re
from collections import defaultdict

from app.db import Conflict, Field
from app.extract.normalize import names_compatible, norm_id, normalize, parse_bool, parse_date
from app.extract.spec import COMPARABLE


def _plain(text: str) -> str:
    """A loss-run cell as words for a sentence: '2021 - PETERBILT -' -> '2021 Peterbilt', 'KEVIN BRANDT' -> 'Kevin Brandt'."""
    words = re.sub(r"\s*-\s*", " ", text or "").strip()
    return " ".join(w.capitalize() if w.isalpha() else w for w in words.split())

# Same real-world fact under a different name on another document type.
ALIASES = {
    ("ifta", "taxpayer_name"): ("applicant", "business_name"),
    ("loss_run", "insured_name"): ("applicant", "business_name"),
}
# When values disagree, which source is suggested (higher wins). Reason shown to the reviewer.
SOURCE_PRIORITY = {"MVR": 5, "DRIVER_LICENSE": 3, "APPLICATION": 4, "IFTA_RETURN": 3, "LOSS_RUN": 3,
                   "DRIVER_SCHEDULE": 2, "VEHICLE_SCHEDULE": 2, "QUOTE_FORM": 1}


def _source(f: Field) -> str:
    if f.method == "human":
        return f"human decision ({f.excerpt[:60]})"
    page = f.page
    if page is None:
        return "human override"
    label = f" ({page.label})" if page.label else ""
    return f"{page.document.filename} p{page.page_no}{label}"


INACTIVE = ("SUPERSEDED", "REPLACED")


def active_fields(db, submission_id) -> list[Field]:
    return db.query(Field).filter(Field.submission_id == submission_id, Field.status.notin_(INACTIVE)).all()


def detect(db, submission_id) -> list[Conflict]:
    fields = active_fields(db, submission_id)
    found: list[Conflict] = _duplicate_mvrs(submission_id, fields)
    # stale MVR values are out before any comparison, so they can't create false conflicts
    fields = [f for f in fields if f.status != "SUPERSEDED"]

    # 1. Same entity + field, different values across documents
    groups = defaultdict(list)
    for f in fields:
        entity_type = f.entity_key.split(":")[0] if f.entity_key else f.entity_type
        target = ALIASES.get((f.entity_type, f.name))
        if target:
            groups[(target[0], target[1])].append(f)
        elif f.name in COMPARABLE.get(entity_type, set()):
            groups[(f.entity_key, f.name)].append(f)
    # driver names: compare composed name per page
    by_driver_page = defaultdict(dict)
    for f in fields:
        if f.entity_type == "driver" and f.name in ("first_name", "last_name", "full_name"):
            by_driver_page[(f.entity_key, f.page_id)][f.name] = f
    for (key, _), parts in by_driver_page.items():
        if "full_name" not in parts and "last_name" in parts:
            groups[(key, "full_name")].append(parts["last_name"])  # carrier for the composed name

    for (key, name), items in groups.items():
        conflict = _compare(submission_id, key, name, items, by_driver_page)
        if conflict:
            found.append(conflict)

    found.extend(_listed_terms_without_loss_runs(submission_id, fields))
    found.extend(_claims_vs_schedules(submission_id, fields))
    _apply_human_decisions(db, submission_id, found)
    for c in found:
        db.add(c)
    return found


# World manufacturer identifiers (first 3 VIN characters) of common heavy-truck makes. Public
# reference data (SAE/NHTSA), used to suggest which source is right when makes disagree.
TRUCK_WMI = {"4V4": "VOLVO", "4V5": "VOLVO", "4VG": "VOLVO", "1FU": "FREIGHTLINER", "1FV": "FREIGHTLINER",
             "3AK": "FREIGHTLINER", "3AL": "FREIGHTLINER", "1XP": "PETERBILT", "2XP": "PETERBILT",
             "1XK": "KENWORTH", "2XK": "KENWORTH", "3HS": "INTERNATIONAL", "1HS": "INTERNATIONAL",
             "3HA": "INTERNATIONAL", "1M1": "MACK", "1M2": "MACK", "5KJ": "WESTERN STAR"}
SCHEDULE_TYPES = {"VEHICLE_SCHEDULE", "DRIVER_SCHEDULE", "QUOTE_FORM", "APPLICATION"}
_MAKES = sorted(set(TRUCK_WMI.values()), key=len, reverse=True)


def _claims_vs_schedules(submission_id, fields) -> list[Conflict]:
    """Loss-run claims checked against the submitted schedules: a claim vehicle whose make differs
    from the scheduled unit with the same VIN, and claim vehicles/drivers that are not scheduled at all
    (the loss run itself may flag them 'unreported')."""
    claims = defaultdict(dict)
    sched_vehicles = defaultdict(dict)
    roster = defaultdict(dict)
    for f in fields:
        if f.entity_type == "claim":
            claims[f.entity_key][f.name] = f
        elif f.entity_type == "vehicle" and f.doc_type in SCHEDULE_TYPES:
            sched_vehicles[f.entity_key][f.name] = f
        elif f.entity_type == "driver" and f.doc_type in SCHEDULE_TYPES:
            roster[f.entity_key][f.name] = f
    roster_names = []
    for parts in roster.values():
        full = parts.get("full_name")
        name = full.value if full else " ".join(p.value for n in ("first_name", "last_name") if (p := parts.get(n)))
        if name:
            roster_names.append(name)

    out = []
    for claim_key, c in claims.items():
        vin_f, veh_f = c.get("vin"), c.get("vehicle")
        if vin_f and len(norm_id(vin_f.value)) >= 11:
            vin = norm_id(vin_f.value)
            sched = sched_vehicles.get(f"vehicle:{vin}")
            claim_make = next((m for m in _MAKES if veh_f and m in veh_f.value.upper()), None)
            if sched and claim_make and sched.get("make") and normalize("make", sched["make"].value) != claim_make:
                wmi = TRUCK_WMI.get(vin[:3])
                values = [{"value": sched["make"].value, "field_id": sched["make"].id, "source": _source(sched["make"]),
                           "doc_type": sched["make"].doc_type},
                          {"value": veh_f.value, "field_id": veh_f.id, "source": _source(veh_f), "doc_type": "LOSS_RUN"}]
                suggested = next((v["field_id"] for v in values if wmi and wmi in v["value"].upper()), None)
                out.append(Conflict(
                    submission_id=submission_id, kind="CONFLICT", entity_key=f"vehicle:{vin}", field_name="make",
                    description=f"Same VIN, different make: schedule vs claim {claim_key.split(':', 1)[1]}",
                    values=values, suggested_field_id=suggested,
                    suggestion_reason=f"VIN prefix {vin[:3]} is {wmi}" if wmi else "no VIN-prefix reference"))
            elif not sched:
                ids = [x for x in (vin_f, veh_f, c.get("unit_reported")) if x]
                out.append(Conflict(
                    submission_id=submission_id, kind="CONFLICT", entity_key=f"vehicle:{vin}", field_name="scheduled",
                    description=(f"Claim {claim_key.split(':', 1)[1]} involves a {_plain(veh_f.value) if veh_f else 'vehicle'} "
                                 f"(VIN {vin}) that isn't on the vehicle list"
                                 + ("; the loss run marks it as an unreported unit"
                                    if c.get("unit_reported") and parse_bool(c["unit_reported"].value) else "")),
                    values=[{"value": x.value, "field_id": x.id, "source": _source(x), "doc_type": "LOSS_RUN"} for x in ids],
                    suggested_field_id=None, suggestion_reason="ask the agent whether this unit is still operated"))
        drv_f = c.get("driver_name")
        if drv_f and roster_names and not any(names_compatible(drv_f.value, n) for n in roster_names):
            ids = [x for x in (drv_f, c.get("driver_dob"), c.get("driver_reported")) if x]
            out.append(Conflict(
                submission_id=submission_id, kind="CONFLICT",
                entity_key=f"driver:{'-'.join(sorted(normalize('full_name', drv_f.value).split()))}",
                field_name="scheduled",
                description=(f"Claim {claim_key.split(':', 1)[1]} names driver {_plain(drv_f.value)}, who isn't on the "
                             "driver list"
                             + (f"; the loss run lists the driver as {c['driver_reported'].value.lower()}"
                                if c.get("driver_reported") else "")),
                values=[{"value": x.value, "field_id": x.id, "source": _source(x), "doc_type": "LOSS_RUN"} for x in ids],
                suggested_field_id=None, suggestion_reason="ask the agent for this driver's status and MVR"))
    return out


def _apply_human_decisions(db, submission_id, found: list[Conflict]) -> None:
    """A conflict on a fact a human already decided is RESOLVED, citing that decision. A conflict a
    human resolved in an earlier run (e.g. 'loss run requested from agent') is carried forward when
    the same conflict reappears after reprocess."""
    humans = {(f.entity_key, f.name): f for f in db.query(Field).filter(
        Field.submission_id == submission_id, Field.method == "human", Field.status == "OK")}
    earlier = {}
    for c in db.query(Conflict).filter(Conflict.submission_id == submission_id, Conflict.status == "REPLACED",
                                       Conflict.description.like("[RESOLVED before reprocess]%")):
        earlier[(c.kind, c.entity_key, c.field_name)] = c
    for c in found:
        if (c.status or "OPEN") != "OPEN":  # new rows have no status until flushed; the column default is OPEN
            continue
        h = humans.get((c.entity_key, c.field_name))
        prior = earlier.get((c.kind, c.entity_key, c.field_name))
        if h:
            c.status = "RESOLVED"
            c.resolution = f"Human decision F{h.id}: '{h.value}' ({h.excerpt[:80]})"
        elif prior and not prior.resolution.startswith("System:"):
            c.status = "RESOLVED"
            c.resolution = f"Carried forward from conflict #{prior.id}: {prior.resolution}"


TERM_MATCH_DAYS = 3  # carriers' term dates differ by a day (AL 12/15 vs cargo 12/16)


def _listed_terms_without_loss_runs(submission_id, fields) -> list[Conflict]:
    """Statement vs package inventory (D-30): a prior term the agent listed (often noted 'LOSS RUNS
    ATTACHED') with no loss run for that term anywhere in the package."""
    runs = []
    for f in fields:
        if f.entity_type == "loss_run" and f.name == "term_start" and (d := parse_date(f.value)):
            runs.append(d)
    terms = defaultdict(dict)
    for f in fields:
        if f.entity_type == "prior_policy":
            terms[f.entity_key][f.name] = f
    out = []
    for key, t in terms.items():
        start = t.get("term_start")
        d = parse_date(start.value) if start else None
        if not d or any(abs((d - r).days) <= TERM_MATCH_DAYS for r in runs):
            continue
        note = t.get("loss_runs_note")
        carrier = t.get("carrier")
        end = t.get("term_end")
        said = f" and says '{note.value}'" if note else ""
        listed = [x for x in (note, start) if x]
        out.append(Conflict(
            submission_id=submission_id, kind="CONFLICT", entity_key=key, field_name="loss_run",
            description=(f"The application lists the {carrier.value if carrier else 'prior'} term "
                         f"{start.value}–{end.value if end else '?'}{said}, but no loss run for that term "
                         f"is in the package ({len(runs)} loss runs received)"),
            values=[{"value": x.value, "field_id": x.id, "source": _source(x), "doc_type": x.doc_type} for x in listed],
            suggested_field_id=None, suggestion_reason="request the missing loss run from the agent"))
    return out


def _display_value(f: Field, name: str, by_driver_page) -> str:
    if name == "full_name" and f.name != "full_name":
        parts = by_driver_page.get((f.entity_key, f.page_id), {})
        return " ".join(p.value for n in ("first_name", "last_name") if (p := parts.get(n)))
    return f.value


def _compare(submission_id, key, name, items, by_driver_page) -> Conflict | None:
    values = []
    for f in items:
        shown = _display_value(f, name, by_driver_page)
        values.append({"value": shown, "field_id": f.id, "source": _source(f), "doc_type": f.doc_type,
                       "page_id": f.page_id, "norm": normalize(name if name != "full_name" else "full_name", shown)})
    # Independent sources = distinct (file, document type). One PDF can bundle several documents
    # (the "MVR" file holds license photos), so a photo and an MVR in the same file still compare.
    distinct_docs = {(v["source"].split(" p")[0], v["doc_type"]) for v in values}
    if len(distinct_docs) < 2:
        return None
    if name in ("full_name", "business_name", "agency_name"):
        clusters = []
        for v in values:
            for c in clusters:
                if names_compatible(c[0]["value"], v["value"]):
                    c.append(v)
                    break
            else:
                clusters.append([v])
        if len(clusters) < 2:
            return None
    else:
        norms = {str(v["norm"]) for v in values if v["norm"] is not None}
        if len(norms) < 2:
            return None
    suggested = _suggest(values)
    label = key if ":" in key else f"{key}"
    return Conflict(
        submission_id=submission_id, kind="CONFLICT", entity_key=label, field_name=name,
        description=f"{name.replace('_', ' ')} differs across documents for {label}",
        values=[{k: v[k] for k in ("value", "field_id", "source", "doc_type")} for v in values],
        suggested_field_id=suggested[0], suggestion_reason=suggested[1])


def _suggest(values) -> tuple[int | None, str]:
    counts = defaultdict(list)
    for v in values:
        counts[str(v["norm"] if v["norm"] is not None else v["value"]).upper()].append(v)
    ranked = sorted(counts.values(), key=lambda vs: (len({x["source"] for x in vs}),
                                                      max(SOURCE_PRIORITY.get(x["doc_type"], 0) for x in vs)),
                    reverse=True)
    top = ranked[0]
    if len(ranked) > 1 and len(ranked[0]) == len(ranked[1]):
        best = max(top, key=lambda x: SOURCE_PRIORITY.get(x["doc_type"], 0))
        return best["field_id"], f"tie between sources; suggesting {best['doc_type']} (higher source priority)"
    return top[0]["field_id"], f"{len(top)} of {len(values)} sources agree"


def _duplicate_mvrs(submission_id, fields) -> list[Conflict]:
    """Two MVRs for the same driver: the newest is current. Every value read from an older MVR page
    is marked SUPERSEDED (kept and shown, never deleted), so rules can't use last year's record.
    This is deterministic (the guideline only accepts MVRs within 90 days), so it is recorded as
    resolved by the system rather than left for a human."""
    mvr_dates = defaultdict(list)
    for f in fields:
        if f.doc_type == "MVR" and f.name == "mvr_order_date":
            mvr_dates[f.entity_key].append(f)
    out = []
    for key, items in mvr_dates.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda f: normalize("mvr_order_date", f.value) or "", reverse=True)
        current, stale = items[0], items[1:]
        stale_pages = {f.page_id for f in stale}
        for f in fields:
            if f.page_id in stale_pages and f.method != "human":
                f.status = "SUPERSEDED"
                f.validation = {**f.validation, "superseded_by_newer_mvr": current.value}
        out.append(Conflict(
            submission_id=submission_id, kind="DUPLICATE", entity_key=key, field_name="mvr",
            description=f"{len(items)} MVRs for the same driver",
            values=[{"value": f.value, "field_id": f.id, "source": _source(f), "doc_type": "MVR"} for f in items],
            suggested_field_id=current.id, suggestion_reason="most recent order date",
            status="RESOLVED",
            resolution=(f"System: using the MVR ordered {current.value}. Older report(s) ordered "
                        f"{', '.join(f.value for f in stale)} marked superseded; still visible, not used by rules.")))
    return out
