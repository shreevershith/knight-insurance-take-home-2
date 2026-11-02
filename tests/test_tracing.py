"""Langfuse tracing (D-52), checked offline: spans go to an in-memory exporter, never to a Langfuse server."""
import uuid

import pytest
from anthropic.types import Message, TextBlock, Usage
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import config, graph, llm, pipeline, tracing
from app.db import SessionLocal, Submission, init_db


@pytest.fixture()
def spans(monkeypatch):
    """Tracing on, exporting to memory, with the app's masking."""
    from langfuse import Langfuse
    exporter = InMemorySpanExporter()
    monkeypatch.delenv("LANGFUSE_TRACING_ENABLED", raising=False)   # conftest turns tracing off for every other test
    lf = Langfuse(public_key=f"pk-test-{uuid.uuid4().hex[:8]}", secret_key="sk-test", base_url="http://127.0.0.1:9",
                  span_exporter=exporter, mask_otel_spans=tracing._mask_otel_spans, tracing_enabled=True)
    monkeypatch.setattr(tracing, "_client", lf)
    monkeypatch.setattr(config, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(config, "LANGFUSE_CAPTURE_CONTENT", False)
    monkeypatch.setattr(lf, "score_current_trace", lambda **kw: None)   # scores need the API; not under test here

    def finished():
        lf.flush()
        return {s.name: dict(s.attributes) for s in exporter.get_finished_spans()}
    return finished


def test_off_without_keys(monkeypatch):
    monkeypatch.setattr(config, "LANGFUSE_ENABLED", False)
    assert tracing.callbacks() == []
    with tracing.trace("process-submission", 1, input={}, tags=[]) as root:
        root.update(output={"x": 1})                                     # a no-op stand-in


def test_document_content_is_not_sent_but_our_summaries_are(spans, monkeypatch):
    with tracing.trace("process-submission", 7, input={"subject": "New business"}, tags=["pipeline"]) as root:
        with tracing.generation("extract-mvr", model="claude-haiku-5-5", metadata={"task": "extract_MVR"}) as g:
            g.update(input={"page": "WHITFIELD, MARCUS  DOB 03/15/1959  DL W38215604720918"}, output={"dob": "03/15/1959"})
        root.update(output={"decision": "REFER"})
    got = spans()
    gen, root = got["extract-mvr"], got["process-submission"]
    assert "WHITFIELD" not in gen["langfuse.observation.input"] and "not captured" in gen["langfuse.observation.input"]
    assert "1959" not in gen["langfuse.observation.output"]
    assert root["langfuse.observation.input"] == '{"subject": "New business"}'      # our summary, kept
    assert gen["session.id"] == root["session.id"] == "submission-7"
    assert gen["langfuse.trace.name"] == "process-submission"


def test_with_capture_on_content_is_sent_pii_masked(spans, monkeypatch):
    monkeypatch.setattr(config, "LANGFUSE_CAPTURE_CONTENT", True)
    with tracing.trace("process-submission", 8, input={}, tags=[]):
        with tracing.generation("extract-mvr", model="claude-haiku-5-5", metadata={}) as g:
            g.update(input={"page": "DOB 03/15/1959 FEIN 75-4418290"})
    sent = spans()["extract-mvr"]["langfuse.observation.input"]
    assert "DOB" in sent and "03/15/1959" not in sent and "75-4418290" not in sent


def test_graph_run_traces_nodes_and_langchain_generations(spans, monkeypatch, tmp_path):
    """A graph run with one real ChatAnthropic call (its HTTP client faked): nodes become spans under
    process-submission, the call becomes a generation named after its task, with the model, and redacted."""
    init_db()
    with SessionLocal() as db:
        s = Submission(subject="graph trace", status="PROCESSING")
        db.add(s)
        db.commit()
        sid = s.id

    class Fake:
        class messages:
            @staticmethod
            def create(**payload):
                return Message(id="m1", type="message", role="assistant", model="claude-haiku-5-5",
                               content=[TextBlock(type="text", text='{"name": "WHITFIELD"}')], stop_reason="end_turn",
                               stop_sequence=None, usage=Usage(input_tokens=50, output_tokens=6))
    from langchain_anthropic import ChatAnthropic
    monkeypatch.setattr(ChatAnthropic, "_client", property(lambda self: Fake()))
    monkeypatch.setattr(config, "LLM_CLIENT", "langchain")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "USAGE_LOG", tmp_path / "usage.jsonl")
    monkeypatch.setattr(config, "llm_configured", lambda: True)
    monkeypatch.setattr(config, "PIPELINE_ENGINE", "graph")
    monkeypatch.setattr(graph, "_memory", None)
    llm._chat_models.clear()

    def stage(name):
        def fn(db, sub, state):
            if name == "extract":
                llm.structured_call(task="extract_MVR", system="read the MVR", schema={"type": "object"},
                                    content=[{"type": "text", "text": "WHITFIELD, MARCUS DOB 03/15/1959"}])
            if name == "analyst":
                sub.status, sub.decision = "DONE", "REFER"
            return {}
        fn.__name__ = f"stage_{name}"
        return fn
    names = ["split", "classify", "extract", "commodities", "cross_check", "escalate_ifta", "confirm_ifta",
             "conflicts", "rules", "analyst"]
    monkeypatch.setattr(pipeline, "STAGES", [stage(n) for n in names])
    token = llm.run_id.set("")
    try:
        graph.run(sid)
    finally:
        llm.run_id.reset(token)
    got = spans()
    assert {"process-submission", "run-pipeline", "split-pages", "extract-values", "route-ifta-check",
            "write-analysis", "extract-mvr"} <= set(got) and "LangGraph" not in got
    assert got["split-pages"]["langfuse.observation.input"] == f'{{"submission_id": {sid}}}'   # node state: safe parts kept
    gen = got["extract-mvr"]
    assert gen["langfuse.observation.type"] == "generation"
    assert gen["langfuse.observation.model.name"] == "claude-haiku-5-5"
    assert "WHITFIELD" not in gen.get("langfuse.observation.input", "") + gen.get("langfuse.observation.output", "")
    assert gen["session.id"] == f"submission-{sid}" and gen["langfuse.trace.name"] == "process-submission"
    assert got["process-submission"]["langfuse.observation.output"] == '{"decision": "REFER", "team": "", "rule_outcomes": {}}'


def test_node_state_keeps_ids_and_counts_but_not_people():
    state = {"submission_id": 9, "failed_ifta": ["2026Q1"],
             "known_drivers": [{"key": "driver:W1", "name": "WHITFIELD, MARCUS", "dob": "03/15/1959", "license": "W1"}],
             "last_action": {"kind": "override", "value": "03/15/1959", "reason": "per MVR", "error": "x"}}
    out = tracing._state_summary(__import__("json").dumps(state))
    assert "WHITFIELD" not in out and "1959" not in out and "per MVR" not in out
    assert '"failed_ifta": ["2026Q1"]' in out and "1 drivers (not captured)" in out and '"kind": "override"' in out
    assert tracing._state_summary('"a page of an MVR"') is None          # not run state: fully redacted instead


def test_router_choices_are_kept():
    assert tracing._state_summary('"reread-ifta-scans"') == '"reread-ifta-scans"'
    assert tracing._state_summary('["confirm-ifta"]') == '["confirm-ifta"]'
    assert tracing._state_summary('"WHITFIELD, MARCUS"') is None
    assert tracing._state_summary("await-review") == "await-review"          # written as a bare string
    assert tracing._state_summary("WHITFIELD, MARCUS DOB 03/15/1959") is None
