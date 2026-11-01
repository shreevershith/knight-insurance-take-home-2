"""Tests run against a throwaway data directory, never the real one."""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="knight_test_")
