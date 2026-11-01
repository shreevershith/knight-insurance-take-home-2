"""Job queue on the database (D-36): no broker, the same Postgres the app already uses.

The web app claims the submission (pipeline.claim: one active job per submission) and enqueues a job.
A worker (app/worker.py) takes the oldest available job with SELECT ... FOR UPDATE SKIP LOCKED, so two
workers never take the same one, and holds it under a lease it renews while the job runs. A worker that
dies stops renewing; once the lease has run out, another worker takes the job over. A failed attempt is
retried after a backoff, up to max_attempts; then the job and the submission end FAILED.

On SQLite (tests, local runs) FOR UPDATE is not emitted; SQLite's single writer gives the same result
for one worker.
"""
from datetime import timedelta

from sqlalchemy import and_, or_

from app import config, pipeline
from app.db import Job, JobLog, SessionLocal, Submission, utcnow
from app.log import get_logger

log = get_logger(__name__)
KINDS = {"run": pipeline.run, "analysis": pipeline.run_analysis}
ACTIVE = ("QUEUED", "RUNNING")


def enqueue(submission_id: int, kind: str = "run") -> int:
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    with SessionLocal() as db:
        job = Job(submission_id=submission_id, kind=kind, max_attempts=config.JOB_MAX_ATTEMPTS)
        db.add(job)
        db.commit()
        return job.id


def claim_next(worker: str) -> tuple[int, int, str, int] | None:
    """(job id, submission id, kind, attempt) of the job this worker now holds, or None if none is ready.
    Ready: QUEUED and past its backoff, or RUNNING with a lease that has run out (its worker died)."""
    while True:
        now = utcnow()
        with SessionLocal() as db:
            job = (db.query(Job)
                   .filter(or_(and_(Job.status == "QUEUED", Job.available_at <= now),
                               and_(Job.status == "RUNNING", Job.lease_until < now)))
                   .order_by(Job.id).with_for_update(skip_locked=True).first())
            if job is None:
                return None
            if job.status == "RUNNING":
                _note(db, job, f"Worker {job.worker} stopped renewing its lease; job taken over by {worker}", "WARN")
                if job.attempts >= job.max_attempts:
                    _give_up(db, job, f"lease expired on the last attempt ({job.attempts} of {job.max_attempts})")
                    db.commit()
                    continue
            job.status, job.worker = "RUNNING", worker
            job.attempts += 1
            job.lease_until = now + timedelta(seconds=config.JOB_LEASE_SECONDS)
            db.commit()
            return job.id, job.submission_id, job.kind, job.attempts


def renew(job_id: int, worker: str) -> bool:
    """Extend the lease. False if this worker no longer holds the job (another one took it over)."""
    with SessionLocal() as db:
        n = (db.query(Job).filter(Job.id == job_id, Job.worker == worker, Job.status == "RUNNING")
             .update({"lease_until": utcnow() + timedelta(seconds=config.JOB_LEASE_SECONDS)},
                     synchronize_session=False))
        db.commit()
        return n == 1


def finish(job_id: int, worker: str) -> str:
    """Record the outcome after the job function returned: the submission's status says how it went
    (the pipeline catches its own errors and ends DONE or FAILED). Returns the job's new status."""
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job.worker != worker or job.status != "RUNNING":
            return job.status  # taken over by another worker after our lease ran out: theirs now
        sub = db.get(Submission, job.submission_id)
        if sub.status == "DONE":
            job.status, job.finished_at, job.lease_until = "DONE", utcnow(), None
        elif job.attempts < job.max_attempts:
            delay = config.JOB_RETRY_BACKOFF_SECONDS * job.attempts
            job.status, job.lease_until = "QUEUED", None
            job.available_at = utcnow() + timedelta(seconds=delay)
            job.error = sub.step
            sub.status, sub.step = "PROCESSING", f"Queued: retry {job.attempts + 1} of {job.max_attempts} in {delay}s"
            _note(db, job, f"Attempt {job.attempts} failed ({job.error}); {sub.step}", "WARN")
        else:
            _give_up(db, job, sub.step)
        db.commit()
        return job.status


def has_active_job(db, submission_id: int) -> bool:
    return db.query(Job.id).filter(Job.submission_id == submission_id, Job.status.in_(ACTIVE)).first() is not None


def _give_up(db, job: Job, why: str) -> None:
    job.status, job.finished_at, job.lease_until, job.error = "FAILED", utcnow(), None, why
    sub = db.get(Submission, job.submission_id)
    sub.status, sub.step = "FAILED", f"Failed after {job.attempts} attempt(s): {why}"[:100]
    _note(db, job, sub.step, "ERROR")


def _note(db, job: Job, message: str, level: str) -> None:
    db.add(JobLog(submission_id=job.submission_id, level=level, message=message))
    log.info(message, submission_id=job.submission_id, job_id=job.id)
