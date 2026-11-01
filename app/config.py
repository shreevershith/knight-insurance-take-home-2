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
DB_URL = f"sqlite:///{(DATA_DIR / 'knight.db').as_posix()}"

CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = _env("CLAUDE_EFFORT", "medium")
CLAUDE_FALLBACKS = _env("CLAUDE_FALLBACKS", "default")
LLM_TIMEOUT_SECONDS = float(_env("LLM_TIMEOUT_SECONDS", "180"))
RULES_FILE = ROOT / _env("RULES_FILE", "app/rules/knight_v1.yaml")

for d in (DATA_DIR, BLOB_DIR, PAGE_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)


def llm_configured() -> bool:
    return bool(_env("ANTHROPIC_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN"))
