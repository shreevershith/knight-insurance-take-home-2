"""D-36: the job queue (claim, lease takeover, retries, restart) and parallel page calls."""
import threading
import time
from datetime import timedelta

import pytest

from app import config, jobs, llm, pipeline, worker
from app.db import Job, SessionLocal, Submission, init_db, utcnow


@pytest.fixture(autouse=True)
def clean_queue():
    init_db()
    with SessionLocal() as db:  # jobs left by other tests would be claimed first
        db.query(Job).delete()
        db.commit()


def _submission(status="PROCESSING") -> int:
    with SessionLocal() as db:
        s = Submission(subject="queue", status=status)
        db.add(s)
        db.commit()
        return s.id


def _set(model, row_id, **values):
    with SessionLocal() as db:
        db.query(model).filter(model.id == row_id).update(values)
        db.commit()


def _get(model, row_id):
    with SessionLocal() as db:
        return db.get(model, row_id)


def test_a_job_is_claimed_once_and_finished_from_the_submission_status():
    sid = _submission()
    job_id = jobs.enqueue(sid)
    assert jobs.claim_next("w1") == (job_id, sid, "run", 1)
    assert jobs.claim_next("w2") is None                      # held: nobody else gets it
    _set(Submission, sid, status="DONE")
    assert jobs.finish(job_id, "w1") == "DONE"


def test_an_expired_lease_hands_the_job_to_another_worker():
    sid = _submission()
    job_id = jobs.enqueue(sid)
    jobs.claim_next("dead")
    _set(Job, job_id, lease_until=utcnow() - timedelta(seconds=1))   # the worker stopped renewing
    assert jobs.claim_next("alive") == (job_id, sid, "run", 2)
    assert not jobs.renew(job_id, "dead")                    # the old worker can't take it back
    assert jobs.finish(job_id, "dead") == "RUNNING"          # and its late finish changes nothing
    assert _get(Job, job_id).worker == "alive"


def test_a_failed_attempt_is_retried_with_backoff_then_fails_for_good(monkeypatch):
    monkeypatch.setattr(config, "JOB_MAX_ATTEMPTS", 2)
    sid = _submission()
    job_id = jobs.enqueue(sid)
    jobs.claim_next("w")
    _set(Submission, sid, status="FAILED", step="Pipeline failed: boom")
    assert jobs.finish(job_id, "w") == "QUEUED"
    assert _get(Submission, sid).status == "PROCESSING"      # still being worked on, not shown as failed
    assert jobs.claim_next("w") is None                      # not before the backoff
    _set(Job, job_id, available_at=utcnow() - timedelta(seconds=1))
    assert jobs.claim_next("w")[3] == 2
    _set(Submission, sid, status="FAILED", step="Pipeline failed: boom")
    assert jobs.finish(job_id, "w") == "FAILED"
    assert _get(Submission, sid).status == "FAILED" and "2 attempt" in _get(Submission, sid).step


def test_web_restart_leaves_queued_work_to_the_workers():
    queued, orphan = _submission(), _submission()
    jobs.enqueue(queued)
    pipeline.reset_interrupted()
    assert _get(Submission, queued).status == "PROCESSING"
    assert _get(Submission, orphan).status == "INTERRUPTED"


def test_worker_runs_a_job_end_to_end(monkeypatch):
    sid = _submission()
    jobs.enqueue(sid)

    def fake_run(submission_id, claimed=False):
        assert claimed
        _set(Submission, submission_id, status="DONE")
    monkeypatch.setitem(jobs.KINDS, "run", fake_run)
    assert worker.run_one("w") is True
    assert worker.run_one("w") is False                      # queue empty
    with SessionLocal() as db:
        assert db.query(Job).filter_by(submission_id=sid).one().status == "DONE"


def test_page_calls_run_in_parallel_in_order_with_the_run_id(monkeypatch):
    monkeypatch.setattr(config, "LLM_CONCURRENCY", 4)
    token = llm.run_id.set("sub9-test")
    seen = set()

    def work(i):
        time.sleep(0.2)
        seen.add((llm.run_id.get(), threading.current_thread().name != "MainThread"))
        if i == 2:
            raise llm.LLMError("page failed")
        return i * 10
    started = time.monotonic()
    try:
        out = pipeline._parallel(work, [0, 1, 2, 3])
    finally:
        llm.run_id.reset(token)
    assert time.monotonic() - started < 0.6                  # 4 x 0.2 s ran at once
    assert out[:2] == [0, 10] and isinstance(out[2], llm.LLMError) and out[3] == 30
    assert seen == {("sub9-test", True)}                     # ledger lines keep the job's run id


def test_ops_page_lists_queue_failures_and_cost():
    from fastapi.testclient import TestClient

    from app.web.main import app
    sid = _submission(status="FAILED")
    _set(Submission, sid, step="Failed after 3 attempt(s): boom")
    jobs.enqueue(sid)
    page = TestClient(app).get("/ops")
    assert page.status_code == 200
    assert "Needs attention" in page.text and "Failed after 3 attempt(s): boom" in page.text
    assert f"#{sid}" in page.text and "Cost per run" in page.text


def test_ops_page_goes_back_to_the_submission_you_came_from():
    from fastapi.testclient import TestClient

    from app.web.main import app
    client = TestClient(app)
    page = client.get("/ops", headers={"referer": "http://testserver/submissions/9"})
    assert page.text.count('href="/submissions/9">← Back to submission #9') == 1   # in the header
    assert 'href="/ops">Operations' not in page.text                              # not a link to itself
    page = client.get("/ops")
    assert page.text.count('href="/">← All submissions') == 1


def test_a_failed_lease_renewal_does_not_stop_the_heartbeat(monkeypatch):
    """A database error during one renewal is logged and retried; the lease is still kept for a long job."""
    sid = _submission()
    jobs.enqueue(sid)
    monkeypatch.setattr(config, "JOB_LEASE_SECONDS", 0.3)            # a beat every 0.1 s
    calls = []
    real_renew = jobs.renew

    def flaky_renew(job_id, w):
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise RuntimeError("connection reset")
        return real_renew(job_id, w)
    monkeypatch.setattr(jobs, "renew", flaky_renew)

    def slow_run(submission_id, claimed=False):
        time.sleep(0.5)
        _set(Submission, submission_id, status="DONE")
    monkeypatch.setitem(jobs.KINDS, "run", slow_run)
    assert worker.run_one("w") is True
    assert len(calls) >= 3                                         # kept beating after the failure
    with SessionLocal() as db:
        assert db.query(Job).filter_by(submission_id=sid).one().status == "DONE"
