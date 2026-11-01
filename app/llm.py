"""Claude client wrapper: structured JSON output, timeouts, retries, on-disk cache, explicit errors.

Results are cached by a hash of the full request, so re-running a submission is repeatable and
free. (Current models reject sampling params like temperature, so the cache is what makes demo
runs repeatable.)
"""
import base64
import hashlib
import json
from pathlib import Path

import anthropic

from app import config
from app.log import get_logger

log = get_logger(__name__)


class LLMError(Exception):
    """Any failure that must surface as MODEL_ERROR instead of a guessed value."""


_client: anthropic.Anthropic | None = None


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
                    max_tokens: int = 16000, ctx: dict | None = None) -> dict:
    """One request -> one JSON object matching `schema`. Raises LLMError on any failure."""
    ctx = ctx or {}
    request = {
        "model": config.CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "output_config": {"effort": config.CLAUDE_EFFORT,
                          "format": {"type": "json_schema", "schema": schema}},
    }
    key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    cache_path = config.CACHE_DIR / f"{task}_{key[:24]}.json"
    if cache_path.exists():
        log.info("llm cache hit", task=task, **ctx)
        return json.loads(cache_path.read_text(encoding="utf-8"))

    kwargs = dict(request)
    if config.CLAUDE_FALLBACKS:
        # Server-side refusal fallback. Not yet a typed SDK parameter in this SDK version.
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["extra_body"] = {"fallbacks": config.CLAUDE_FALLBACKS}
        api = client().beta.messages
    else:
        api = client().messages

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

    usage = response.usage
    log.info("llm call", task=task, model=response.model, request_id=response._request_id,
             input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, **ctx)
    cache_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data
