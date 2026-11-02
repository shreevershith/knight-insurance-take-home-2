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
CACHE_DIR = DATA_DIR / _env("LLM_CACHE_DIR", "llm_cache")  # a fresh dir = a fully measured run
USAGE_LOG = DATA_DIR / "llm_usage.jsonl"  # one line per model call: the cost ledger (RUN_LOG.md)
# SQLite by default; set DATABASE_URL (e.g. postgresql+psycopg://...) for Postgres. Same schema.
DB_URL = _env("DATABASE_URL") or f"sqlite:///{(DATA_DIR / 'knight.db').as_posix()}"

CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = _env("CLAUDE_EFFORT", "medium")
CLAUDE_FALLBACKS = _env("CLAUDE_FALLBACKS", "default")
LLM_TIMEOUT_SECONDS = float(_env("LLM_TIMEOUT_SECONDS", "180"))
# Page calls of one job run this many at a time (D-36): a run is almost all model wait, one page at a
# time. Bounded by the API rate limit, not by CPU. 1 = sequential.
LLM_CONCURRENCY = max(1, int(_env("LLM_CONCURRENCY", "6")))

# Jobs (D-36). "thread": the web process runs each job in a background thread (no worker needed).
# "queue": the web app only enqueues; app/worker.py processes run them (docker-compose.yml).
JOB_MODE = _env("JOB_MODE", "thread")
JOB_LEASE_SECONDS = int(_env("JOB_LEASE_SECONDS", "120"))   # a worker renews it every third of this
JOB_MAX_ATTEMPTS = int(_env("JOB_MAX_ATTEMPTS", "3"))
JOB_RETRY_BACKOFF_SECONDS = int(_env("JOB_RETRY_BACKOFF_SECONDS", "30"))  # x attempt number
WORKER_POLL_SECONDS = float(_env("WORKER_POLL_SECONDS", "2"))

# Per-task model routing (D-31, measured $0.62/submission vs $2.37 on Opus everywhere). The model fits
# how well code can check the task: classify/extract are verified against the page (cheap model),
# transcribing scans is only partly checkable (mid model), the analyst is judgment (CLAUDE_MODEL).
# Override one tier with LLM_MODEL_<TIER> / LLM_EFFORT_<TIER>. A cheap-tier result that fails a code
# check is re-run on the escalation model, so a cheaper model can cost a re-run, not a wrong value.
TIERS = ("classify", "transcribe", "extract", "analyst", "draft")
TIER_DEFAULTS = {"classify": "claude-haiku-5-5", "transcribe": "claude-sonnet-5-5",
                 "extract": "claude-haiku-5-5", "analyst": CLAUDE_MODEL,
                 "draft": "claude-sonnet-5-5"}  # wording the agent reply from findings code chose (D-48)
RUN_LABEL = _env("LLM_RUN_LABEL")  # tags ledger lines for an experiment, e.g. "haiku"
ESCALATION_MODEL = _env("LLM_ESCALATION_MODEL", "claude-opus-5-5")
ESCALATION_EFFORT = _env("LLM_ESCALATION_EFFORT", "medium")
# USD per million tokens (input, output)
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}


def route(tier: str) -> tuple[str, str]:
    """(model, effort) for a task tier."""
    t = tier.upper()
    return _env(f"LLM_MODEL_{t}", TIER_DEFAULTS[tier]), _env(f"LLM_EFFORT_{t}", CLAUDE_EFFORT)


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


def stored_path(path) -> str:
    """Path as stored in the DB: relative to DATA_DIR, so the data folder (or a container volume)
    can move without breaking rows."""
    path = Path(path)
    try:
        return path.resolve().relative_to(DATA_DIR.resolve()).as_posix()
    except ValueError:
        return str(path)  # outside DATA_DIR (shouldn't happen): keep as given


def data_path(stored: str) -> Path:
    """Inverse of stored_path. Rows written before paths were relative hold absolute paths."""
    path = Path(stored)
    return path if path.is_absolute() else DATA_DIR / path
