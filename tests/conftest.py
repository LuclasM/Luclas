"""
tests/conftest.py — shared fixtures for the concurrency regression suite.

These tests exist for one specific reason: a multi-round review found a
handful of business-logic bugs that are invisible from reading the code and
invisible from a single-threaded manual run — they only show up under real
concurrency (cron's cross-process channel lock, TaskMemory.maybe_compress()'s
claim-before-summarize race, memory/embedder.py's lazy-load race,
ask_user()'s per-session lock, the Discord adapter's reconnect-loop globals).
Each was found once and fixed once; the only thing that stops a future
unrelated change from silently reintroducing one is a test that actually
exercises the race, which is what lives here. This is deliberately NOT a
general test suite — most of the codebase's other bugs (validation logic,
string formatting, etc.) were one-off fixes verified ad hoc and don't need a
permanent regression test the same way a race condition does.
"""
import os
import sys
import tempfile

_LUCLAS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "luclas")
if _LUCLAS_DIR not in sys.path:
    sys.path.insert(0, _LUCLAS_DIR)

import pytest


@pytest.fixture
def isolated_db(monkeypatch):
    """A fresh temp DATA_DIR/DB_PATH with the real schema applied, with every
    module that captured DB_PATH by value at import time (`from config import
    DB_PATH`, rather than reading config.DB_PATH dynamically) patched to
    match — config.py, memory/database.py, and cron_runner.py all do this."""
    import config
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    monkeypatch.setattr(config, "DATA_DIR", tmpdir)
    monkeypatch.setattr(config, "DB_PATH", db_path)

    from memory import database as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    db_module.init_db()

    yield {"tmpdir": tmpdir, "db_path": db_path}
