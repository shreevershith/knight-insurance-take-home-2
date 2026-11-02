"""Tests run against a throwaway data directory, never the real one."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="knight_test_")
# Never the app's DATABASE_URL from .env: a throwaway SQLite file, or a database named for tests.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "")
# Tests never send traces to the Langfuse project in .env (tests/test_tracing.py checks the masking offline).
os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
