"""Settings from environment (.env). Secrets never live in code."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DATA_DIR = ROOT / _env("DATA_DIR", "data")
BLOB_DIR = DATA_DIR / "blobs"
PAGE_DIR = DATA_DIR / "pages"
CACHE_DIR = DATA_DIR / "llm_cache"
USAGE_LOG = DATA_DIR / "llm_usage.jsonl"  # one line per model call: the cost ledger (RUN_LOG.md)
DB_URL = f"sqlite:///{(DATA_DIR / 'knight.db').as_posix()}"

CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = _env("CLAUDE_EFFORT", "medium")
CLAUDE_FALLBACKS = _env("CLAUDE_FALLBACKS", "default")
LLM_TIMEOUT_SECONDS = float(_env("LLM_TIMEOUT_SECONDS", "180"))

# Per-task model routing (D-31). Every tier defaults to CLAUDE_MODEL / CLAUDE_EFFORT; override one tier
# with LLM_MODEL_<TIER> / LLM_EFFORT_<TIER>. A cheap-tier result that fails a code check is re-run on
# the escalation model, so a cheaper model can only cost a re-run, never a silently wrong value.
TIERS = ("classify", "transcribe", "extract", "analyst")
RUN_LABEL = _env("LLM_RUN_LABEL")  # tags ledger lines for an experiment, e.g. "haiku"
ESCALATION_MODEL = _env("LLM_ESCALATION_MODEL", "claude-opus-5-5")
ESCALATION_EFFORT = _env("LLM_ESCALATION_EFFORT", "medium")
# USD per million tokens (input, output)
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}


def route(tier: str) -> tuple[str, str]:
    """(model, effort) for a task tier."""
    t = tier.upper()
    return _env(f"LLM_MODEL_{t}", CLAUDE_MODEL), _env(f"LLM_EFFORT_{t}", CLAUDE_EFFORT)


def supports_fallbacks(model: str) -> bool:
    # Haiku 5.5 has no server-side fallback: a list 400s and "default" does nothing.
    return bool(CLAUDE_FALLBACKS) and not model.startswith("claude-haiku")


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000
RULES_FILE = ROOT / _env("RULES_FILE", "app/rules/knight_v1.yaml")

for d in (DATA_DIR, BLOB_DIR, PAGE_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)


def llm_configured() -> bool:
    return bool(_env("ANTHROPIC_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN"))
