"""Test fixtures for the server test suite.

Puts server/ on sys.path (instead of installing it as a package, to match
the flat module layout used by the deliverable) and points the app at a
fresh temporary SQLite file per test session so tests never touch the real
proctoring.db.
"""
from __future__ import annotations

import os
import sys
import tempfile

SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "server"))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

_db_fd, _db_path = tempfile.mkstemp(suffix=".db", prefix="proctoring_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
os.environ["HEARTBEAT_TIMEOUT_SECONDS"] = "20"

# Keep evidence snapshots written during tests out of the real project
# directory (config.py's default is server/evidence_storage).
_evidence_dir = tempfile.mkdtemp(prefix="proctoring_test_evidence_")
os.environ["EVIDENCE_STORAGE_DIR"] = _evidence_dir

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init_database():
    from database.db import init_db

    init_db()
    yield
    os.close(_db_fd)
    os.remove(_db_path)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin_headers():
    return {"X-Admin-Api-Key": "test-admin-key"}
