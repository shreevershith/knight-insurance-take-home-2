"""Human review actions: overrides, conflict resolutions, rule re-runs.

Kept out of the web routes so the worker and tests use the same code. Every human decision is
stored as a `method="human"` Field keyed by (entity_key, field name), never by row id, so it
survives re-extraction (pipeline.reapply_human_decisions). Originals are never overwritten.
"""
from app.db import Conflict, Field, RuleResult, Submission, audit
from app.extract.spec import SPEC
from app.rules.engine import evaluate
from app.web.labels import entity_labels, field_label, label_for

FACT_FIELDS = {name for groups in SPEC.values() for _, _, fields in groups.values() for name in fields}


class ReviewError(ValueError):
    pass


def human_field(db, *, submission_id, entity_key, name, value, reason, actor, based_on: Field | None):
    """A human decision about one fact. based_on: the extracted value it replaces or accepts."""
    f = Field(submission_id=submission_id, page_id=based_on.page_id if based_on else None,
              doc_type=based_on.doc_type if based_on else "", entity_type=entity_key.split(":")[0],
              entity_key=entity_key, name=name, value=value.strip(), excerpt=f"{actor}: {reason.strip()}",
              method="human", confidence=1.0, evidence_verified=True,
              validation={"human_decision": True}, status="OK",
              overrides_field_id=based_on.id if based_on else None)
    db.add(f)
    # any earlier human decision on the same fact is replaced (kept, not deleted)
    for old in db.query(Field).filter(Field.submission_id == submission_id, Field.entity_key == entity_key,
                                      Field.name == name, Field.method == "human", Field.status == "OK"):
        if old is not f:
            old.status = "SUPERSEDED"
    db.flush()
    return f


def _not_processing(db, submission_id: int) -> Submission:
    """Human edits wait for a running job: a reprocess would replace the rows being edited."""
    sub = db.get(Submission, submission_id)
    if sub.status == "PROCESSING":
        raise ReviewError("the submission is being processed; try again when it is done")
    return sub


def _analysis_out_of_date(sub: Submission, why: str) -> None:
    """The AI findings were written before this change; say so instead of showing them as current."""
    if sub.summary and not sub.summary.get("error"):
        sub.summary = {**sub.summary, "out_of_date": why}  # reassign: JSON columns don't track mutation


def _entity_name(db, submission_id: int, entity_key: str) -> str:
    """'Applicant', 'Luis E Morales', 'Kevin Brandt (not on the driver list)': how the page names an entity."""
    fields = db.query(Field).filter(Field.submission_id == submission_id, Field.entity_key == entity_key).all()
    return label_for(entity_key, entity_labels(fields))


def _fact_name(db, submission_id: int, entity_key: str, name: str) -> str:
    """'Agency phone (Applicant)', 'Date of birth (Luis E Morales)': how the page names this fact."""
    label = field_label(name)
    return f"{label[:1].upper()}{label[1:]} ({_entity_name(db, submission_id, entity_key)})"


def override_field(db, field_id: int, value: str, reason: str, actor: str) -> Submission:
    if not reason.strip():
        raise ReviewError("a reason is required")
    old = db.get(Field, field_id)
    if old is None:
        raise ReviewError("field not found")
    _not_processing(db, old.submission_id)
    new = human_field(db, submission_id=old.submission_id, entity_key=old.entity_key, name=old.name,
                      value=value, reason=reason, actor=actor, based_on=old)
    superseded = _supersede_extracted(db, old.submission_id, old.entity_key, old.name)
    audit(db, old.submission_id, actor, "field.override", f"field:{old.id} {old.entity_key}.{old.name}",
          before={"value": old.value, "field_id": old.id},
          after={"value": new.value, "field_id": new.id, "superseded_field_ids": superseded}, reason=reason.strip())
    # a disagreement about this fact is settled by the edit: close it, as a reprocess would
    for c in db.query(Conflict).filter(Conflict.submission_id == old.submission_id, Conflict.status == "OPEN",
                                       Conflict.entity_key == old.entity_key, Conflict.field_name == old.name):
        c.status = "RESOLVED"
        c.resolution = f"{actor} corrected the value to '{new.value}': {reason.strip()}"
    sub = db.get(Submission, old.submission_id)
    evaluate(db, sub, actor=actor)
    _analysis_out_of_date(sub, f"{_fact_name(db, sub.id, old.entity_key, old.name)} was changed by {actor}")
    return sub


def resolve_conflict(db, conflict_id: int, field_id: int, reason: str, actor: str) -> Submission:
    if not reason.strip():
        raise ReviewError("a reason is required")
    c = db.get(Conflict, conflict_id)
    if c is None or c.status != "OPEN":
        raise ReviewError("conflict not found or not open")
    chosen = next((v for v in c.values if v["field_id"] == field_id), None)
    if chosen is None:
        raise ReviewError("field is not one of the conflict's values")
    _not_processing(db, c.submission_id)
    accepted = db.get(Field, field_id)
    superseded, human_id = [], None
    if c.field_name in FACT_FIELDS:
        # The accepted value becomes a human fact on the CANONICAL key (e.g. applicant.business_name,
        # even when the accepted value came from an IFTA taxpayer_name), so the rules read it.
        h = human_field(db, submission_id=c.submission_id, entity_key=c.entity_key, name=c.field_name,
                        value=chosen["value"], reason=reason, actor=actor, based_on=accepted)
        human_id = h.id
        superseded = _supersede_extracted(db, c.submission_id, c.entity_key, c.field_name,
                                          extra_ids=[v["field_id"] for v in c.values if v["field_id"] != field_id])
    c.status = "RESOLVED"
    if c.field_name in ("scheduled", "loss_run"):  # nothing to choose between: the person records what they did
        c.resolution = f"{actor} acknowledged: {reason.strip()}"
    else:
        c.resolution = f"{actor} accepted '{chosen['value']}' from {chosen['source']}: {reason.strip()}"
    audit(db, c.submission_id, actor, "conflict.resolve", f"conflict:{c.id} {c.entity_key}.{c.field_name}",
          before={"status": "OPEN", "values": c.values},
          after={"accepted": chosen, "human_field_id": human_id, "superseded_field_ids": superseded},
          reason=reason.strip())
    sub = db.get(Submission, c.submission_id)
    evaluate(db, sub, actor=actor)
    if c.field_name in ("scheduled", "loss_run"):
        why = f"the note about {_entity_name(db, sub.id, c.entity_key)} was acknowledged by {actor}"
    else:
        why = f"the disagreement on {_fact_name(db, sub.id, c.entity_key, c.field_name)} was resolved by {actor}"
    _analysis_out_of_date(sub, why)
    return sub


def rerun_rules(db, submission_id: int, actor: str) -> Submission:
    """Apply the current rules file. The AI summary is marked out of date only if what it was written about
    changed: the decision, the team, or any rule's outcome. A new rules version that only rewords a message
    or a quoted clause doesn't make the findings stale."""
    sub = _not_processing(db, submission_id)
    before = (sub.decision, sub.team, _outcomes(db, submission_id))
    evaluate(db, sub, actor=actor)
    db.flush()
    after = (sub.decision, sub.team, _outcomes(db, submission_id))
    if after != before:
        changed = sorted(r for r in set(before[2]) | set(after[2]) if before[2].get(r) != after[2].get(r))
        _analysis_out_of_date(sub, f"rules re-run: decision {before[0]} -> {after[0]}"
                                   + (f"; outcome changed for {', '.join(changed)}" if changed else ""))
    return sub


def _outcomes(db, submission_id: int) -> dict[str, str]:
    """rule_id -> outcome of the latest rules run."""
    last = (db.query(RuleResult).filter(RuleResult.submission_id == submission_id)
            .order_by(RuleResult.id.desc()).first())
    if last is None:
        return {}
    return {r.rule_id: r.outcome for r in db.query(RuleResult).filter(RuleResult.submission_id == submission_id,
                                                                      RuleResult.run_id == last.run_id)}


def _supersede_extracted(db, submission_id, entity_key, name, extra_ids=()) -> list[int]:
    """Extracted (non-human) values of the fact that a human just decided are marked SUPERSEDED."""
    q = db.query(Field).filter(Field.submission_id == submission_id, Field.method != "human",
                               Field.status.in_(("OK", "LOW_CONFIDENCE")))
    ids = []
    for f in q:
        if (f.entity_key == entity_key and f.name == name) or f.id in extra_ids:
            f.status = "SUPERSEDED"
            f.validation = {**(f.validation or {}), "superseded_by_human": True}
            ids.append(f.id)
    return ids
