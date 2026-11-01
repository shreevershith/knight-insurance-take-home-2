"""Resolved view of a submission for the rules engine.

For each entity field the rules read ONE value: a human override if there is one, otherwise the
value from the most authoritative source (ties: highest confidence). Each value carries the field
ids it came from, so every rule result links back to its evidence.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from app.conflicts import SOURCE_PRIORITY, active_fields
from app.db import Conflict, Document, Field, Page
from app.extract.normalize import normalize

SCHEDULE_TYPES = {"VEHICLE_SCHEDULE", "DRIVER_SCHEDULE"}


@dataclass
class Value:
    raw: str | None
    norm: object
    field_ids: list[int] = field(default_factory=list)


class SubmissionView:
    def __init__(self, db, submission_id: int):
        self.db = db
        self.submission_id = submission_id
        self.fields: list[Field] = active_fields(db, submission_id)
        self.field_by_id: dict[int, Field] = {f.id: f for f in self.fields}
        self.by_entity: dict[str, list[Field]] = defaultdict(list)
        for f in self.fields:
            self.by_entity[f.entity_key].append(f)
        self.open_conflicts = db.query(Conflict).filter(Conflict.submission_id == submission_id,
                                                        Conflict.status == "OPEN").all()
        # Pages we could not read. A value "missing" from these may simply be unread.
        self.failed_pages = (db.query(Page).join(Document)
                             .filter(Document.submission_id == submission_id,
                                     Page.status.in_(("MODEL_ERROR", "UNREADABLE"))).all())

    def unread(self, doc_types: set[str]) -> list[str]:
        """Failed pages that are (or, if never classified, might be) one of doc_types."""
        return [f"{p.document.filename} p{p.page_no} ({p.status})" for p in self.failed_pages
                if not p.doc_type or p.doc_type in doc_types]

    def value(self, entity_key: str, name: str, doc_types: set[str] | None = None) -> Value:
        candidates = [f for f in self.by_entity.get(entity_key, []) if f.name == name
                      and (doc_types is None or f.doc_type in doc_types or f.method == "human")]
        if not candidates:
            return Value(None, None, [])
        human = [f for f in candidates if f.method == "human"]
        # human decision > verified value > low-confidence value; then source authority, then confidence
        best = max(human, key=lambda f: f.id) if human else max(
            candidates, key=lambda f: (f.status == "OK", SOURCE_PRIORITY.get(f.doc_type, 0), f.confidence))
        return Value(best.value, normalize(name, best.value), [best.id])

    def entities(self, prefix: str, doc_types: set[str] | None = None) -> list[str]:
        keys = []
        for key, items in self.by_entity.items():
            if key.startswith(prefix) and (doc_types is None or any(f.doc_type in doc_types for f in items)):
                keys.append(key)
        return sorted(keys)

    def scheduled_vehicles(self) -> list[str]:
        """Units listed on a vehicle schedule (not vehicles merely mentioned in a loss run)."""
        return self.entities("vehicle:", SCHEDULE_TYPES)

    def anchor_date(self, field_name: str) -> Value:
        """The date windows are measured from (D-07): the application's own date first."""
        from_app = self.value("applicant", field_name, {"APPLICATION"})
        return from_app if from_app.raw else self.value("applicant", field_name)
