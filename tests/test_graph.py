"""PIPELINE_ENGINE=graph (D-51): the LangGraph graph runs the same stages as pipeline.run, in the same order,
branches to the IFTA re-read only when needed, pauses for review, and resumes with review actions."""
import pytest

from app import config, graph, llm, pipeline, review
from app.db import SessionLocal, Submission, init_db

ORDER = ["split", "classify", "extract", "commodities", "cross_check", "escalate_ifta", "confirm_ifta",
         "conflicts", "rules", "analyst"]


@pytest.fixture()
def world(monkeypatch):
    """A submission, fake stages that record what ran (and the run id they saw), recorded review calls."""
    init_db()
    with SessionLocal() as db:
        s = Submission(subject="graph", status="PROCESSING")
        db.add(s)
        db.commit()
        sid = s.id
    ran, calls = [], []
    failed_ifta = {"value": []}

    def fake(name):
        def stage(db, sub, state):
            ran.append((name, llm.run_id.get()))
            if name == "extract":
                return {"known_drivers": [{"key": "driver:D1", "name": "A B", "dob": "", "license": "D1"}]}
            if name == "cross_check":
                return {"failed_ifta": list(failed_ifta["value"])}
            if name == "escalate_ifta":
                return {"failed_ifta": []}
            if name == "analyst":
                sub.status = "DONE"
            return {}
        stage.__name__ = f"stage_{name}"
        return stage
    stages = [fake(n) for n in ORDER]
    monkeypatch.setattr(pipeline, "STAGES", stages)
    monkeypatch.setattr(pipeline, "stage_escalate_ifta", stages[ORDER.index("escalate_ifta")])
    monkeypatch.setattr(config, "PIPELINE_ENGINE", "graph")
    monkeypatch.setattr(config, "route", lambda tier: ("claude-sonnet-5-5", "medium"))
    monkeypatch.setattr(graph, "_memory", None)

    def recorder(kind):
        def fn(db, *args):
            calls.append((kind, args))
            if args and args[-1] == "broken":
                raise review.ReviewError("a reason is required")
        return fn
    monkeypatch.setattr(review, "override_field", recorder("override"))
    monkeypatch.setattr(review, "rerun_rules", recorder("rerun"))
    monkeypatch.setattr(pipeline, "_analyze_and_finish", lambda db, sub: ran.append(("reanalyze", "")))
    token = llm.run_id.set("")   # a run sets the cost ledger's run id; other tests expect it unset
    yield sid, ran, calls, failed_ifta
    llm.run_id.reset(token)


def test_same_stages_same_order_and_a_pause_for_review(world):
    sid, ran, _, _ = world
    graph.run(sid)
    names = [n for n, _ in ran]
    assert names == [n for n in ORDER if n != "escalate_ifta"]          # no IFTA problem: no re-read
    assert all(rid.startswith(f"sub{sid}-") for _, rid in ran)          # the cost ledger's run id reaches nodes
    assert graph.waiting_for_review(sid)


def test_code_engine_runs_the_identical_sequence(world, monkeypatch):
    sid, ran, _, failed = world
    failed["value"] = ["2026Q1"]
    monkeypatch.setattr(config, "PIPELINE_ENGINE", "code")
    pipeline.run(sid, claimed=True)
    code_order = [n for n, _ in ran]
    ran.clear()
    monkeypatch.setattr(config, "PIPELINE_ENGINE", "graph")
    graph.run(sid)
    assert [n for n, _ in ran] == code_order == ORDER                    # IFTA failed: both took the re-read


def test_review_actions_resume_the_graph_and_it_pauses_again(world):
    sid, ran, calls, _ = world
    graph.run(sid)
    graph.apply_review(sid, {"kind": "override", "field_id": 7, "value": "x", "reason": "agent", "actor": "uw"})
    assert calls == [("override", (7, "x", "agent", "uw"))] and graph.waiting_for_review(sid)
    graph.apply_review(sid, {"kind": "rerun_rules", "actor": "uw"})
    assert calls[-1] == ("rerun", (sid, "uw")) and graph.waiting_for_review(sid)


def test_a_refused_action_raises_and_leaves_the_graph_waiting(world):
    sid, _, calls, _ = world
    graph.run(sid)
    with pytest.raises(review.ReviewError, match="reason"):
        graph.apply_review(sid, {"kind": "override", "field_id": 7, "value": "x", "reason": "", "actor": "broken"})
    assert graph.waiting_for_review(sid)
    graph.apply_review(sid, {"kind": "override", "field_id": 7, "value": "x", "reason": "ok", "actor": "uw"})
    assert calls[-1] == ("override", (7, "x", "ok", "uw"))


def test_regenerate_goes_through_the_analyst_and_pauses_again(world):
    sid, ran, _, _ = world
    assert graph.regenerate(sid) is False                                # nothing paused yet
    graph.run(sid)
    assert graph.regenerate(sid) is True
    assert ran[-1][0] == "reanalyze" and graph.waiting_for_review(sid)


def test_reprocess_starts_a_fresh_thread(world):
    sid, ran, _, _ = world
    graph.run(sid)
    ran.clear()
    graph.run(sid)
    assert [n for n, _ in ran][0] == "split" and graph.waiting_for_review(sid)


def test_without_a_paused_graph_review_calls_the_function_directly(world, monkeypatch):
    sid, _, calls, _ = world
    monkeypatch.setattr(config, "PIPELINE_ENGINE", "code")
    graph.apply_review(sid, {"kind": "rerun_rules", "actor": "uw"})
    assert calls == [("rerun", (sid, "uw"))]
