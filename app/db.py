"""Data model. SQLite for the prototype; the schema is portable to Postgres unchanged."""
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from app.config import DB_URL

# check_same_thread is a SQLite-only option (the web app reads from request threads and a worker thread)
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Submission(Base):
    __tablename__ = "submission"
    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sender: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    email_sha256: Mapped[str] = mapped_column(String(64), default="")
    # RECEIVED -> PROCESSING -> DONE | FAILED
    status: Mapped[str] = mapped_column(String(20), default="RECEIVED")
    step: Mapped[str] = mapped_column(String(100), default="")
    decision: Mapped[str] = mapped_column(String(20), default="")
    team: Mapped[str] = mapped_column(String(50), default="")
    rules_version: Mapped[str] = mapped_column(String(50), default="")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)  # AI analyst output

    documents: Mapped[list["Document"]] = relationship(back_populates="submission", cascade="all, delete-orphan")


class Document(Base):
    """One file: the email attachment, or a member unpacked from a zip (parent_id set)."""
    __tablename__ = "document"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submission.id"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("document.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    blob_path: Mapped[str] = mapped_column(String(1000))
    media_type: Mapped[str] = mapped_column(String(100), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("document.id"), nullable=True)
    # OK | CONTAINER | DUPLICATE | UNREADABLE | UNSUPPORTED
    status: Mapped[str] = mapped_column(String(20), default="OK")
    error: Mapped[str] = mapped_column(Text, default="")

    submission: Mapped[Submission] = relationship(back_populates="documents")
    pages: Mapped[list["Page"]] = relationship(back_populates="document", cascade="all, delete-orphan",
                                               order_by="Page.page_no")


class Page(Base):
    """Classification and extraction run per page: one PDF can bundle several document types."""
    __tablename__ = "page"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    page_no: Mapped[int] = mapped_column(Integer)  # 1-based; sheets of an XLSX are pages too
    label: Mapped[str] = mapped_column(String(200), default="")  # e.g. sheet name
    has_text_layer: Mapped[bool] = mapped_column(Boolean, default=False)
    text: Mapped[str] = mapped_column(Text, default="")        # text layer, or vision transcript
    text_source: Mapped[str] = mapped_column(String(30), default="")  # text_layer | vision_transcript | xlsx
    image_path: Mapped[str] = mapped_column(String(1000), default="")
    doc_type: Mapped[str] = mapped_column(String(50), default="")
    classify_method: Mapped[str] = mapped_column(String(30), default="")
    classify_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    classify_reason: Mapped[str] = mapped_column(Text, default="")
    # PENDING | OK | UNREADABLE | MODEL_ERROR | UNCLASSIFIED | LOW_CONFIDENCE | SKIPPED
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    error: Mapped[str] = mapped_column(Text, default="")

    document: Mapped[Document] = relationship(back_populates="pages")


class Field(Base):
    """One extracted value with its evidence. Never edited in place: overrides add a new row."""
    __tablename__ = "field"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submission.id"), index=True)
    page_id: Mapped[int | None] = mapped_column(ForeignKey("page.id"), nullable=True)
    doc_type: Mapped[str] = mapped_column(String(50), default="")
    entity_type: Mapped[str] = mapped_column(String(30))   # applicant | driver | vehicle | loss_run | ifta | coverage ...
    entity_key: Mapped[str] = mapped_column(String(200))   # resolved key, e.g. driver:DL#, vehicle:VIN
    name: Mapped[str] = mapped_column(String(100))
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    excerpt: Mapped[str] = mapped_column(Text, default="")
    evidence_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    method: Mapped[str] = mapped_column(String(30))        # xlsx | llm_text | llm_vision | human
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)  # {check_name: passed?}
    # OK | LOW_CONFIDENCE | SUPERSEDED (replaced by a human override)
    status: Mapped[str] = mapped_column(String(20), default="OK")
    overrides_field_id: Mapped[int | None] = mapped_column(ForeignKey("field.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    page: Mapped["Page | None"] = relationship()


class Conflict(Base):
    __tablename__ = "conflict"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submission.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))          # CONFLICT | DUPLICATE | MISSING
    entity_key: Mapped[str] = mapped_column(String(200))
    field_name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    values: Mapped[list] = mapped_column(JSON, default=list)  # [{value, field_id, source}]
    suggested_field_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    suggestion_reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN | RESOLVED
    resolution: Mapped[str] = mapped_column(Text, default="")


class RuleResult(Base):
    __tablename__ = "rule_result"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submission.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    rule_id: Mapped[str] = mapped_column(String(30))
    rules_version: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(300), default="")
    source_clause: Mapped[str] = mapped_column(Text, default="")
    # PASS | REFER | DECLINE | NOT_EVALUATED | FLAG
    outcome: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text, default="")
    input_field_ids: Mapped[list] = mapped_column(JSON, default=list)
    waived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    """Append-only. Nothing in the app updates or deletes these rows."""
    __tablename__ = "audit_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int | None] = mapped_column(ForeignKey("submission.id"), nullable=True, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(60))
    target: Mapped[str] = mapped_column(String(200), default="")
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")


class JobLog(Base):
    """Progress log shown in the UI while the pipeline runs."""
    __tablename__ = "job_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submission.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    message: Mapped[str] = mapped_column(Text)


class Job(Base):
    """A queued unit of work (D-36). The web app enqueues; a worker claims it with SELECT ... FOR UPDATE
    SKIP LOCKED and holds it under a lease it keeps renewing (app/jobs.py)."""
    __tablename__ = "job"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submission.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # run | analysis
    # QUEUED -> RUNNING -> DONE | FAILED (a failed attempt goes back to QUEUED until max_attempts)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)  # retry backoff
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker: Mapped[str] = mapped_column(String(100), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def init_db() -> None:
    Base.metadata.create_all(engine)


def audit(db, submission_id, actor, action, target="", before=None, after=None, reason=""):
    db.add(AuditEvent(submission_id=submission_id, actor=actor, action=action, target=target,
                      before=before or {}, after=after or {}, reason=reason))
