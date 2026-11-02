"""Langfuse tracing (D-52). Off unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set.

What a trace is here (Langfuse best practices: one trace per unit of work, a session for work with human steps
in between):
- `process-submission`: one pipeline run. Its children are the LangGraph nodes and every model call as a
  `generation` (model, tokens, cost), from the LangChain integration (`callbacks()`); with the code engine or the
  SDK client the same spans and generations are created here. At the end the run gets scores computed by our
  own code checks (`score_run`): how much of what the AI read was verified, escalations, cost, decision.
- `apply-review`: one underwriter action (correction, conflict decision, rules re-run), with user_id.
- `regenerate-analysis`: one re-run of the AI summary.
All three carry session_id `submission-<id>`, so a submission's run and its later reviews read as one story.

Privacy: prompts and model outputs contain driver names, licence numbers and dates of birth. By default
(`LANGFUSE_CAPTURE_CONTENT` unset) every observation's input and output is replaced at export, except the
summaries this module writes itself (ids, counts, decisions, document types). With it set to true, content is
sent with the same PII masking as the logs.
"""
import json
from contextlib import contextmanager
from typing import Any

from app import config
from app.log import get_logger, mask_pii

log = get_logger(__name__)

SAFE = "safe_summary"           # metadata key on observations written here: their input/output is never redacted
_INPUT, _OUTPUT = "langfuse.observation.input", "langfuse.observation.output"
_SAFE_ATTR = f"langfuse.observation.metadata.{SAFE}"
_client = None


def enabled() -> bool:
    return config.LANGFUSE_ENABLED


def client():
    """The Langfuse client, created on first use (after config loaded .env), with export-time masking."""
    global _client
    if _client is None:
        from langfuse import Langfuse
        _client = Langfuse(mask_otel_spans=_mask_otel_spans)
    return _client


def _mask_otel_spans(*, params):
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch
    patches = {}
    for ident, span in params.spans.items():
        if span.attributes.get(_SAFE_ATTR) == json.dumps(True):
            continue
        updates = {}
        for key in (_INPUT, _OUTPUT):
            value = span.attributes.get(key)
            if not isinstance(value, str) or not value:
                continue
            if config.LANGFUSE_CAPTURE_CONTENT:
                masked = mask_pii(value)
                if masked != value:
                    updates[key] = masked
            elif (summary := _state_summary(value)) is not None:
                if summary != value:
                    updates[key] = summary
            else:
                updates[key] = json.dumps(f"[not captured: {len(value)} characters; LANGFUSE_CAPTURE_CONTENT=true sends it]")
        if updates:
            patches[ident] = OtelSpanPatch(set_attributes=updates)
    return MaskOtelSpansResult(span_patches=patches) if patches else None


class _Noop:
    """Stands in for an observation when tracing is off."""
    def update(self, **kwargs) -> None:
        pass


@contextmanager
def trace(name: str, submission_id: int, *, input: dict, tags: list[str], user_id: str | None = None):
    """The root observation of one unit of work, with session, tags and user propagated to every child."""
    if not enabled():
        yield _Noop()
        return
    from langfuse import propagate_attributes
    lf = client()
    meta = {"submission_id": str(submission_id), "pipeline_engine": config.PIPELINE_ENGINE,
            "llm_client": config.LLM_CLIENT}
    with lf.start_as_current_observation(name=name, as_type="span", input=input, metadata={SAFE: True, **meta}) as obs, \
            propagate_attributes(session_id=f"submission-{submission_id}", trace_name=name, user_id=user_id,
                                 tags=tags, metadata=meta):
        try:
            yield obs
        except Exception as exc:
            obs.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}"[:500])
            raise
    lf.flush()   # workers and the web app are long-lived; flush so a finished run shows up at once


@contextmanager
def step(name: str):
    """A pipeline stage as a span (the graph engine gets these from the LangGraph integration instead)."""
    if not enabled():
        yield _Noop()
        return
    with client().start_as_current_observation(name=name, as_type="span") as obs:
        yield obs


@contextmanager
def generation(name: str, *, model: str, metadata: dict):
    """A model call sent by the SDK client (LangChain's calls are traced by the integration)."""
    if not enabled():
        yield _Noop()
        return
    with client().start_as_current_observation(name=name, as_type="generation", model=model,
                                               metadata=metadata) as obs:
        yield obs


def callbacks() -> list:
    """LangChain/LangGraph callbacks: nodes become spans and ChatAnthropic calls become generations, nested
    under the current trace."""
    if not enabled():
        return []
    from langfuse.langchain import CallbackHandler
    # tied to this client by its key: with more than one client in a process, get_client() would skip tracing
    return [CallbackHandler(public_key=client()._resources.public_key)]


GENERATION_NAMES = {"classify_text": "classify-page-text", "classify_vision": "read-page-image",
                    "classify_commodities": "classify-commodities", "analyst": "generate-analysis",
                    "agent_reply": "draft-agent-reply"}


def generation_name(task: str) -> str:
    """Stable, low-cardinality, verb-first names: 'analyst' -> 'generate-analysis', 'extract_MVR' -> 'extract-mvr'."""
    return GENERATION_NAMES.get(task, task.lower().replace("_", "-"))


def _node_names() -> set[str]:
    from app import graph, pipeline
    return set(pipeline.STEP_NAMES.values()) | {graph.REVIEW, graph.REANALYZE, "__end__"}


def _state_summary(value: str) -> str | None:
    """A graph node's state with the personal parts left out (driver names, dates of birth and licences in
    known_drivers; corrected values and reasons in last_action). None if it isn't run state."""
    try:
        state = json.loads(value)
    except ValueError:
        state = value                    # the integration writes a router's choice as a bare string
    choice = state if isinstance(state, list) else [state]
    if choice and all(isinstance(c, str) and c in _node_names() for c in choice):
        return value                     # a router's choice: the next node's name ("reread-ifta-scans" or a list)
    if not isinstance(state, dict) or not set(state) <= {"submission_id", "known_drivers", "failed_ifta", "last_action"}:
        return None
    out = dict(state)
    if "known_drivers" in out:
        out["known_drivers"] = f"{len(out['known_drivers'] or [])} drivers (not captured)"
    if isinstance(out.get("last_action"), dict):
        out["last_action"] = {k: v for k, v in out["last_action"].items() if k in ("kind", "error")}
    return json.dumps(out)


def score_run(db, sub, state: dict, run_id: str) -> None:
    """Scores on the current trace from our own checks, so a dashboard can answer: can we trust what the AI read,
    what did it cost, what was decided."""
    if not enabled():
        return
    from app import llm
    from app.conflicts import active_fields
    from app.db import Conflict, latest_rule_results
    read = [f for f in active_fields(db, sub.id) if f.method in ("llm_text", "llm_vision")
            and "excerpt_in_source" in (f.validation or {})]
    verified = sum(1 for f in read if f.validation["excerpt_in_source"])
    rules = latest_rule_results(db, sub.id)
    summary = sub.summary or {}
    items = summary.get("concerns", []) + summary.get("recommendations", [])
    totals = llm.run_totals(run_id)
    calls = totals["calls"] or 1
    numeric = {
        "evidence-verified-rate": round(verified / len(read), 4) if read else 1.0,
        "low-confidence-values": sum(1 for f in active_fields(db, sub.id) if f.status == "LOW_CONFIDENCE"),
        "ifta-unreconciled": len(state.get("failed_ifta") or []),
        "escalations": totals["escalations"],
        "analyst-unsupported-items": sum(1 for i in items if i.get("unsupported")),
        "commodity-disagreements": sum(1 for r in rules if "confirm what is hauled" in (r.message or "")),
        "open-conflicts": db.query(Conflict).filter(Conflict.submission_id == sub.id, Conflict.status == "OPEN").count(),
        "run-cost-usd": round(totals["cost_usd"], 5),
        "cache-hit-rate": round(totals["cache_hits"] / calls, 4),
    }
    lf = client()
    for name, value in numeric.items():
        lf.score_current_trace(name=name, value=value, data_type="NUMERIC")
    lf.score_current_trace(name="decision", value=sub.decision or "NONE", data_type="CATEGORICAL")
    lf.score_current_trace(name="analyst-ok", value=0 if summary.get("error") else 1, data_type="BOOLEAN")


def score_review(kind: str, *, field: str = "", doc_type: str = "", decision_before: str, decision_after: str) -> None:
    """Scores on an apply-review trace: what kind of action, which field a person corrected, and whether the
    decision moved. Corrections are the record of where extraction goes wrong."""
    if not enabled():
        return
    lf = client()
    lf.score_current_trace(name="review-action", value=kind, data_type="CATEGORICAL")
    if field:
        lf.score_current_trace(name="corrected-field", value=field, data_type="CATEGORICAL",
                               metadata={"doc_type": doc_type})
    lf.score_current_trace(name="decision-changed", value=int(decision_before != decision_after),
                           data_type="BOOLEAN", comment=f"{decision_before} -> {decision_after}")


def safe_input(sub) -> dict[str, Any]:
    """A run's input for the trace: the email as received, without document contents."""
    return {"submission_id": sub.id, "subject": sub.subject,
            "files": sum(1 for d in sub.documents if d.status != "CONTAINER")}


def finish_run(db, sub, state: dict, run_id: str, root) -> None:
    """End of a run: scores from our checks, and the decision as the trace's output."""
    score_run(db, sub, state, run_id)
    root.update(output=safe_output(db, sub))


def safe_output(db, sub) -> dict[str, Any]:
    """A run's output for the trace: what a reviewer needs at a glance, no personal data."""
    from app.db import latest_rule_results
    counts: dict[str, int] = {}
    for r in latest_rule_results(db, sub.id):
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    return {"decision": sub.decision, "team": sub.team, "rule_outcomes": counts}
