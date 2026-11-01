"""Entity resolution: give every extracted item a stable key so the same driver, vehicle or
policy term found in several documents lines up.

Drivers: license number; else name + DOB. Vehicles: VIN. Loss runs: line + policy number.
IFTA: period end. Child items (violations, claims, IFTA rows) take their parent's key.
"""
from collections import defaultdict

from app.extract.normalize import coverage_line, looks_like_vin, name_tokens, names_compatible, norm_id, normalize


def _items(facts):
    """Group facts by (group, index) -> {field: value}."""
    items = defaultdict(dict)
    for f in facts:
        items[(f["group"], f["index"])][f["field"]] = f["value"]
    return items


def _driver_name(d: dict) -> str:
    if d.get("full_name"):
        return d["full_name"]
    return " ".join(x for x in (d.get("first_name"), d.get("last_name")) if x)


def assign_keys(facts: list[dict], page_ctx: dict, known_drivers: list[dict]) -> None:
    """Sets fact['entity_key'] in place. page_ctx carries keys from earlier pages of the same document
    (e.g. an IFTA table page continuing the summary page). known_drivers collects
    {key, name, dob, license} across the submission for name+DOB matching."""
    items = _items(facts)
    keys = {}
    for (group, index), d in items.items():
        etype = next(f["entity_type"] for f in facts if f["group"] == group and f["index"] == index)
        if etype == "driver":
            keys[(group, index)] = _driver_key(d, known_drivers)
        elif etype == "vehicle":
            vin = d.get("vin")
            keys[(group, index)] = (f"vehicle:{norm_id(vin)}" if vin and looks_like_vin(vin)
                                    else f"vehicle:unit-{d.get('unit_number') or index + 1}@{page_ctx['doc_id']}")
        elif etype == "loss_run":
            # the line as a code (AL/MTC/APD), not as printed: 'COMMERCIAL AUTO LIABILITY' is AL (held-out, HO1)
            line = coverage_line(d.get("line")) or (d.get("line") or "?").upper()
            keys[(group, index)] = f"loss_run:{line}:{norm_id(d.get('policy_number')) or page_ctx['doc_id']}"
            page_ctx["loss_run"] = keys[(group, index)]
        elif etype == "ifta":
            period = normalize("period_end", d.get("period_end")) or page_ctx.get("ifta_period")
            keys[(group, index)] = f"ifta:{period or page_ctx['doc_id']}"
            page_ctx["ifta_period"] = period
        elif etype == "prior_policy":
            keys[(group, index)] = f"prior_policy:{normalize('term_start', d.get('term_start')) or index}"
        elif etype in ("applicant", "coverage", "operations"):
            keys[(group, index)] = etype
        elif etype == "commodity":
            keys[(group, index)] = f"commodity:{page_ctx['doc_id']}:{index}"
        else:
            keys[(group, index)] = None  # child items resolved below

    page_driver = next((k for (g, i), k in keys.items() if k and k.startswith("driver:")), None)
    if page_driver:
        page_ctx["driver"] = page_driver
    for (group, index), d in items.items():
        if keys[(group, index)] is not None:
            continue
        etype = next(f["entity_type"] for f in facts if f["group"] == group and f["index"] == index)
        if etype in ("violation", "license_event"):
            keys[(group, index)] = f"{etype}:{page_ctx.get('driver', '?')}:{index}"
        elif etype == "claim":
            keys[(group, index)] = f"claim:{norm_id(d.get('claim_number')) or index}"
        elif etype == "ifta_row":
            j = normalize("jurisdiction", d.get("jurisdiction")) or index
            sur = "-SURCHARGE" if d.get("surcharge") else ""
            keys[(group, index)] = f"ifta_row:{page_ctx.get('ifta_period', '?')}:{j}{sur}"
        else:
            keys[(group, index)] = f"{etype}:{page_ctx['doc_id']}:{group}:{index}"

    for f in facts:
        f["entity_key"] = keys[(f["group"], f["index"])]


def _driver_key(d: dict, known: list[dict]) -> str:
    lic = norm_id(d.get("license_number"))
    name = _driver_name(d)
    dob = normalize("dob", d.get("dob"))
    if lic:
        key = f"driver:{lic}"
    else:
        match = next((k for k in known if names_compatible(k["name"], name)
                      and (not dob or not k["dob"] or k["dob"] == dob)), None)
        key = match["key"] if match else f"driver:{'-'.join(sorted(name_tokens(name))) or 'unknown'}"
    if not any(k["key"] == key for k in known):
        known.append({"key": key, "name": name, "dob": dob, "license": lic})
    return key
