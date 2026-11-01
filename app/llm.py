"""Claude client wrapper: structured JSON output, timeouts, retries, on-disk cache, explicit errors.

Results are cached by a hash of the full request, so re-running a submission is repeatable and
free. (Current models reject sampling params like temperature, so the cache is what makes demo
runs repeatable.)
"""
import base64
import contextvars
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from app import config
from app.log import get_logger

log = get_logger(__name__)


class LLMError(Exception):
    """Any failure that must surface as MODEL_ERROR instead of a guessed value."""


_client: anthropic.Anthropic | None = None
# Set by the pipeline per job; every ledger line carries it so cost can be summed per run. A ContextVar,
# not a global: each job runs in its own thread, and two jobs at once must not log under each other's id.
run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")

TIER_OF = {"classify_text": "classify", "classify_vision": "transcribe", "analyst": "analyst"}


def tier_for(task: str) -> str:
    return TIER_OF.get(task, "extract")  # extract_<doc type> tasks


def _ledger(**row) -> None:
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_id": run_id.get(), **row}
    with open(config.USAGE_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not config.llm_configured():
            raise LLMError("ANTHROPIC_API_KEY is not set (see .env.example)")
        _client = anthropic.Anthropic(timeout=config.LLM_TIMEOUT_SECONDS, max_retries=3)
    return _client


def image_block(path: str | Path) -> dict:
    data = Path(path).read_bytes()
    suffix = Path(path).suffix.lower()
    media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".webp": "image/webp"}.get(suffix, "image/png")
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.standard_b64encode(data).decode()}}


def structured_call(*, task: str, system: str, content: list[dict], schema: dict,
                    max_tokens: int = 16000, ctx: dict | None = None, escalate: bool = False,
                    reason: str = "") -> dict:
    """One request -> one JSON object matching `schema`. Raises LLMError on any failure.

    The model comes from the task's tier (config.route); escalate=True uses the escalation model
    instead (a re-run after a cheap-tier result failed a code check; `reason` says which)."""
    ctx = ctx or {}
    tier = tier_for(task)
    model, effort = (config.ESCALATION_MODEL, config.ESCALATION_EFFORT) if escalate else config.route(tier)
    request = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "output_config": {"effort": effort,
                          "format": {"type": "json_schema", "schema": schema}},
    }
    key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    cache_path = config.CACHE_DIR / f"{task}_{key[:24]}.json"
    meta = {"task": task, "tier": tier, "model": model, "effort": effort, "escalated": escalate,
            "escalation_reason": reason}
    if cache_path.exists():
        log.info("llm cache hit", task=task, **ctx)
        _ledger(**meta, cache_hit=True, input_tokens=0, output_tokens=0, cost_usd=0.0, seconds=0.0)
        return json.loads(cache_path.read_text(encoding="utf-8"))

    kwargs = dict(request)
    if config.supports_fallbacks(model):
        # Server-side refusal fallback. Not yet a typed SDK parameter in this SDK version.
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["extra_body"] = {"fallbacks": config.CLAUDE_FALLBACKS}
        api = client().beta.messages
    else:
        api = client().messages

    started = time.monotonic()
    try:
        response = api.create(**kwargs)
    except anthropic.AuthenticationError as exc:
        raise LLMError(f"authentication failed: {exc.message}") from exc
    except anthropic.BadRequestError as exc:
        raise LLMError(f"bad request: {exc.message}") from exc
    except anthropic.RateLimitError as exc:
        raise LLMError("rate limited after retries") from exc
    except anthropic.APITimeoutError as exc:
        raise LLMError(f"timed out after {config.LLM_TIMEOUT_SECONDS}s") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(f"connection error: {exc}") from exc

    usage = response.usage
    _ledger(**meta, cache_hit=False, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cost_usd=round(config.cost_usd(model, usage.input_tokens, usage.output_tokens), 5),
            seconds=round(time.monotonic() - started, 1), stop_reason=response.stop_reason)
    if response.stop_reason == "refusal":
        raise LLMError("model declined the request")
    if response.stop_reason == "max_tokens":
        raise LLMError("output truncated at max_tokens")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise LLMError(f"no text block in response (stop_reason={response.stop_reason})")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"invalid JSON from model: {exc}") from exc

    log.info("llm call", task=task, model=response.model, request_id=response._request_id,
             input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, **ctx)
    cache_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data
