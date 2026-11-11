"""Test setup. Tests run against a REAL PostgreSQL (row locks, triggers and
constraints are the thing under test; an in-memory fake would prove nothing).

    TEST_DATABASE_URL defaults to postgresql://ledgerline:ledgerline@localhost:5432/ledgerline_test

The schema is dropped and recreated once per session. Tests never clean up:
each one opens its own users and accounts, and because every entry balances,
the global invariants (sum of balances == 0, proof passes) must still hold
across ALL tests - test_zz_final.py checks exactly that at the end.
"""

from __future__ import annotations

import os
import socket
import threading
import time

from app.config import _load_dotenv

_load_dotenv()                                           # pick up TEST_DATABASE_URL from .env if present
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://ledgerline:ledgerline@localhost:5432/ledgerline_test")
os.environ["APP_ENV"] = "test"
os.environ["LEDGER_TEST_HOOKS"] = "1"
os.environ["PASSWORD_HASH_N"] = "1024"                   # fast hashing in tests only
os.environ["DB_POOL_MAX"] = "40"
os.environ["TRUST_PROXY"] = "0"
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-ledgerline-tests-only-000")
os.environ.pop("ANTHROPIC_API_KEY", None)               # tests never call a real AI provider

import pytest  # noqa: E402

from app import cli, db  # noqa: E402
from app.security import login_throttle, signup_throttle  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def database():
    with db.connect() as conn:
        conn.execute("DROP SCHEMA IF EXISTS ledgerline CASCADE")
    cli.migrate()
    yield
    db.close_pool()


@pytest.fixture(autouse=True)
def _fresh_throttle():
    login_throttle.reset()
    signup_throttle.reset()
    yield


@pytest.fixture(scope="session")
def server(database):
    """A real uvicorn server in this process, so HTTP tests hit the real stack."""
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started:
        if time.time() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)
