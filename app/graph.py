"""The pipeline as a LangGraph graph (D-51; PIPELINE_ENGINE=graph).

Nodes are the same stage functions `pipeline.run` calls in order (pipeline.STAGES), so both engines do the same
work; the graph adds what LangGraph is for:
- a conditional edge: IFTA rows that don't add up go through a re-read on the escalation model;
- a checkpointer (Postgres in Docker, in-memory with SQLite): the run's state is saved after every node;
- human review as interrupt/resume: after the analyst the graph pauses at `human_review` and waits, durably,
  for the underwriter. Each correction, conflict decision or rules re-run resumes it with that action
  (`apply_review`); a regenerate resumes it from a worker (`regenerate`). Then it pauses again.

The job queue (app/jobs.py) still decides who runs a job and takes over a crashed one; a reprocess starts the
graph's thread afresh. One thread per submission: "submission-<id>".
"""
import threading
from contextlib import contextmanager
from typing import Any, TypedDict

from langchain_core.runnables import RunnableLambda
from langgraph.graph import START, StateGraph
from langgraph.types import Command, interrupt

from app import config, llm, pipeline, review, tracing
from app.db import SessionLocal, Submission
from app.log import get_logger

log = get_logger(__name__)


class RunState(TypedDict, total=False):
    submission_id: int
    known_drivers: list[dict]     # from extraction, for the IFTA re-read
    failed_ifta: list[str]        # IFTA quarters whose rows don't add up
    last_action: dict             # the review action the graph was last resumed with


def _node(stage):
    """A stage as a node: its own session, committed after the stage (in `run` one session spans all stages)."""
    def node(state: RunState) -> dict:
        with SessionLocal() as db:
            sub = db.get(Submission, state["submission_id"])
            updates = stage(db, sub, state) or {}
            db.commit()
            return updates
    node.__name__ = pipeline.step_name(stage)
    return node


REVIEW, REANALYZE = "await-review", "rewrite-analysis"


def _human_review(state: RunState) -> dict:
    """Pause until the underwriter acts; apply the action with the same review functions the web used before."""
    sid = state["submission_id"]
    action = interrupt({"submission_id": sid, "waiting_for": "underwriter review"})
    if action["kind"] != "regenerate":             # a regenerate goes to the analyst (edge below)
        try:
            _apply(sid, action)
        except review.ReviewError as exc:
            # recorded, not raised: a node that raises keeps its resume value, and the next resume would get the
            # refused action back; this way the graph pauses again cleanly and apply_review reports the error
            return {"last_action": {**action, "error": str(exc)}}
    return {"last_action": action}


def _apply(submission_id: int, action: dict) -> None:
    """One review action with the review functions; raises ReviewError."""
    with SessionLocal() as db:
        kind = action["kind"]
        if kind == "override":
            review.override_field(db, action["field_id"], action["value"], action["reason"], action["actor"])
        elif kind == "resolve":
            review.resolve_conflict(db, action["conflict_id"], action["field_id"], action["reason"], action["actor"])
        elif kind == "rerun_rules":
            review.rerun_rules(db, submission_id, action["actor"])
        else:
            raise review.ReviewError(f"unknown review action {kind!r}")
        db.commit()


def _reanalyze(state: RunState) -> dict:
    with SessionLocal() as db:
        sub = db.get(Submission, state["submission_id"])
        pipeline.progress(db, sub, "AI analyst: re-running on the reviewed values")
        pipeline._analyze_and_finish(db, sub)
        db.commit()
    return {}


def build(checkpointer=None):
    g = StateGraph(RunState)
    names = []
    for stage in pipeline.STAGES:
        node = _node(stage)
        g.add_node(node.__name__, node)
        names.append(node.__name__)
    g.add_node(REVIEW, _human_review)
    g.add_node(REANALYZE, _reanalyze)
    step = {short: pipeline.STEP_NAMES[short] for short in ("cross_check", "escalate_ifta", "confirm_ifta", "analyst")}

    g.add_edge(START, names[0])
    for a, b in zip(names, names[1:]):
        if step["escalate_ifta"] in (a, b):
            continue
        g.add_edge(a, b)
    # the one branch of the run: re-read IFTA scans whose rows don't add up, else go straight on
    # (named routers, so traces show what decided the path)
    g.add_conditional_edges(step["cross_check"], RunnableLambda(
        lambda s: step["escalate_ifta"] if pipeline.needs_ifta_escalation(s) else step["confirm_ifta"],
        name="route-ifta-check"), [step["escalate_ifta"], step["confirm_ifta"]])
    g.add_edge(step["escalate_ifta"], step["confirm_ifta"])
    g.add_edge(step["analyst"], REVIEW)
    g.add_conditional_edges(REVIEW, RunnableLambda(
        lambda s: REANALYZE if s["last_action"]["kind"] == "regenerate" else REVIEW,
        name="route-review-action"), [REANALYZE, REVIEW])
    g.add_edge(REANALYZE, REVIEW)
    return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------- checkpointer

_memory = None
_memory_lock = threading.Lock()
_pg_ready = False


@contextmanager
def _checkpointer():
    """Postgres when the app runs on Postgres (shared by the web process and every worker), else in-memory."""
    global _memory, _pg_ready
    if config.DB_URL.startswith("postgresql"):
        from langgraph.checkpoint.postgres import PostgresSaver
        url = "postgresql://" + config.DB_URL.split("://", 1)[1]   # psycopg form, without "+psycopg"
        with PostgresSaver.from_conn_string(url) as saver:
            if not _pg_ready:
                saver.setup()
                _pg_ready = True
            yield saver
    else:
        from langgraph.checkpoint.memory import InMemorySaver
        with _memory_lock:
            _memory = _memory or InMemorySaver()
        yield _memory


def _config(submission_id: int) -> dict:
    return {"configurable": {"thread_id": f"submission-{submission_id}"}, "recursion_limit": 1000}


def _invoke(saver, payload, submission_id: int, run_name: str):
    """Run or resume the submission's thread. The Langfuse callback (D-52) turns each node into a span and each
    ChatAnthropic call into a generation; run_name names the graph run in the trace."""
    return build(saver).invoke(payload, {**_config(submission_id), "callbacks": tracing.callbacks(),
                                         "run_name": run_name})


def waiting_for_review(submission_id: int) -> bool:
    """True when this submission's graph is paused at await-review."""
    if config.PIPELINE_ENGINE != "graph":
        return False
    with _checkpointer() as saver:
        snapshot = build(saver).get_state(_config(submission_id))
        return bool(snapshot.next) and snapshot.next[0] == REVIEW and bool(snapshot.interrupts)


# ---------------------------------------------------------------- entry points

def run(submission_id: int) -> None:
    """A full run (pipeline.run with PIPELINE_ENGINE=graph): a fresh thread, every stage, then the pause."""
    pipeline._start_run_id(submission_id)
    with SessionLocal() as db:
        trace_input = tracing.safe_input(db.get(Submission, submission_id))
    with tracing.trace("process-submission", submission_id, input=trace_input, tags=["pipeline", "engine:graph"]) as root, \
            _checkpointer() as saver:
        saver.delete_thread(f"submission-{submission_id}")   # a reprocess starts over, like pipeline.run
        try:
            state = _invoke(saver, {"submission_id": submission_id}, submission_id, "run-pipeline")
            with SessionLocal() as db:
                tracing.finish_run(db, db.get(Submission, submission_id), state, llm.run_id.get(), root)
        except Exception as exc:
            with SessionLocal() as db:
                pipeline.fail(db, submission_id, exc)
                db.commit()
            tracing.mark_error(root, exc)


def apply_review(submission_id: int, action: dict[str, Any]) -> None:
    """A review action from the web page. Resumes the paused graph with it; a submission without a paused graph
    (processed by the code engine, or before D-51) gets the same review function directly. Raises ReviewError."""
    if waiting_for_review(submission_id):
        with _checkpointer() as saver:
            state = _invoke(saver, Command(resume=action), submission_id, "resume-with-review")
        if error := state.get("last_action", {}).get("error"):
            raise review.ReviewError(error)
    else:
        _apply(submission_id, action)


def regenerate(submission_id: int) -> bool:
    """Resume the paused graph with 'regenerate' (from a worker). False if there is no paused graph."""
    if not waiting_for_review(submission_id):
        return False
    with _checkpointer() as saver:
        _invoke(saver, Command(resume={"kind": "regenerate"}), submission_id, "resume-with-regenerate")
    return True


def mermaid() -> str:
    """The graph as Mermaid, for docs/ARCHITECTURE.md."""
    return build().get_graph().draw_mermaid()
