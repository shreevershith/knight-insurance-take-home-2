"""LLM_CLIENT=langchain (D-51): ChatAnthropic must send the very request the SDK path sends, and its reply must
feed the same cache, ledger and errors. No API calls: the SDK client underneath is replaced."""
import json

import pytest
from anthropic.types import Message, TextBlock, Usage

from app import config, llm


def _reply(text='{"ok": true}', stop="end_turn"):
    return Message(id="msg_1", type="message", role="assistant", model="claude-haiku-5-5", content=[
        TextBlock(type="text", text=text)], stop_reason=stop, stop_sequence=None,
        usage=Usage(input_tokens=120, output_tokens=8))


@pytest.fixture()
def captured(monkeypatch, tmp_path):
    """Both clients' requests, recorded where the SDK would send them."""
    sent = []

    class Messages:
        def create(self, **payload):
            sent.append(payload)
            return _reply()

    class Client:
        messages = Messages()
        beta = type("Beta", (), {"messages": Messages()})()

    monkeypatch.setattr(llm, "client", lambda: Client())
    from langchain_anthropic import ChatAnthropic
    monkeypatch.setattr(ChatAnthropic, "_client", property(lambda self: Client()))
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "USAGE_LOG", tmp_path / "usage.jsonl")
    monkeypatch.setattr(config, "llm_configured", lambda: True)
    llm._chat_models.clear()
    return sent


def _call(task="extract_MVR", **kw):
    return llm.structured_call(task=task, system="sys", schema={"type": "object"},
                               content=[{"type": "text", "text": "page text"},
                                        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                     "data": "iVBORw0KGgo="}}], **kw)


@pytest.mark.parametrize("task", ["extract_MVR", "analyst"])   # Haiku without fallbacks; Opus with them
def test_langchain_sends_the_same_request_as_the_sdk(captured, monkeypatch, task):
    monkeypatch.setattr(config, "LLM_CLIENT", "sdk")
    assert _call(task) == {"ok": True}
    for f in config.CACHE_DIR.glob("*.json"):
        f.unlink()                                         # a cache miss again, for the other client
    monkeypatch.setattr(config, "LLM_CLIENT", "langchain")
    assert _call(task) == {"ok": True}
    sdk, lc = captured
    assert json.dumps(lc, sort_keys=True) == json.dumps(sdk, sort_keys=True)
    rows = [json.loads(line) for line in config.USAGE_LOG.read_text().splitlines()]
    assert [(r["client"], r["input_tokens"], r["output_tokens"], r["stop_reason"]) for r in rows] == [
        ("sdk", 120, 8, "end_turn"), ("langchain", 120, 8, "end_turn")]


def test_langchain_reply_problems_raise_the_same_errors(captured, monkeypatch):
    monkeypatch.setattr(config, "LLM_CLIENT", "langchain")
    from langchain_anthropic import ChatAnthropic

    class Truncated:
        class messages:
            @staticmethod
            def create(**payload):
                return _reply('{"cut', stop="max_tokens")
    monkeypatch.setattr(ChatAnthropic, "_client", property(lambda self: Truncated()))
    with pytest.raises(llm.LLMError, match="truncated"):
        _call()
