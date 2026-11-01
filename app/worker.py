"""Job worker (D-36): python -m app.worker

Takes jobs from the queue one at a time and runs them; run several workers to process several
submissions at once (docker compose up --scale worker=3). While a job runs, a heartbeat thread renews
its lease. On SIGTERM/SIGINT (docker stop) the worker finishes polling; a job cut off mid-run is taken
over by another worker once its lease runs out, and the pipeline's reprocess keeps all human work.
"""
import os
import signal
import socket
import threading
import uuid

from app import config, jobs
from app.db import init_db
from app.log import get_logger, setup_logging

log = get_logger(__name__)
_stop = threading.Event()


def run_one(worker: str) -> bool:
    """Claim and run one job. False when none was ready."""
    claimed = jobs.claim_next(worker)
    if claimed is None:
        return False
    job_id, submission_id, kind, attempt = claimed
    log.info("job started", job_id=job_id, submission_id=submission_id, kind=kind, attempt=attempt)
    done = threading.Event()

    def heartbeat():
        while not done.wait(config.JOB_LEASE_SECONDS / 3):
            if not jobs.renew(job_id, worker):
                log.warning("lease lost", job_id=job_id)
                return
    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        jobs.KINDS[kind](submission_id, claimed=True)
    finally:
        done.set()
    status = jobs.finish(job_id, worker)
    log.info("job finished", job_id=job_id, submission_id=submission_id, status=status)
    return True


def main() -> None:
    setup_logging()
    init_db()
    # unique per process start: a restarted container keeps its hostname and pid 1
    worker = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())
    log.info("worker started", worker=worker, poll_seconds=config.WORKER_POLL_SECONDS,
             concurrency=config.LLM_CONCURRENCY)
    while not _stop.is_set():
        if not run_one(worker):
            _stop.wait(config.WORKER_POLL_SECONDS)
    log.info("worker stopped", worker=worker)


if __name__ == "__main__":
    main()
