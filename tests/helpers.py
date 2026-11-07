"""Shared test helpers."""

from __future__ import annotations

import itertools
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

from app import db
from app.ledger import admin_ops, engine, proof
from app.ledger.engine import Actor

RUPEE = 100
_counter = itertools.count(1)
_admin: Actor | None = None
_lock = threading.Lock()
PASSWORD = "correct-horse-battery"


def key() -> str:
    return str(uuid.uuid4())


def unique(prefix: str) -> str:
    return f"{prefix}{next(_counter):04d}{uuid.uuid4().hex[:4]}"


def admin() -> Actor:
    global _admin
    with _lock:
        if _admin is None:
            name = unique("adm")
            user = admin_ops.create_user(None, name, "Test Admin", "admin", PASSWORD)
            _admin = Actor(user["id"], name, "admin")
        return _admin


def make_user(role: str = "holder", prefix: str | None = None) -> Actor:
    name = unique(prefix or role[:3])
    user = admin_ops.create_user(admin().id, name, f"Test {role} {name}", role, PASSWORD)
    return Actor(user["id"], name, role)


def open_account(owner: Actor, rupees: int = 0, name: str = "Test account") -> str:
    acct = admin_ops.open_account(admin().id, owner.username, name)
    if rupees:
        out = engine.deposit(admin(), acct["account_no"], rupees * RUPEE, "test funding", key())
        assert out.body["status"] == "posted", out.body
    return acct["account_no"]


def balance(account_no: str) -> int:
    with db.connect() as conn:
        return conn.execute("SELECT balance FROM accounts WHERE account_no = %s", (account_no,)).fetchone()["balance"]


def account_id(account_no: str) -> int:
    with db.connect() as conn:
        return conn.execute("SELECT id FROM accounts WHERE account_no = %s", (account_no,)).fetchone()["id"]


def entries(where: str = "TRUE", params: tuple = ()) -> list[dict]:
    with db.connect() as conn:
        return conn.execute(f"SELECT * FROM journal WHERE {where} ORDER BY seq", params).fetchall()


def backstops() -> int:
    """How many times PostgreSQL had to refuse a write the app tried. A correct
    app never needs this, so every race test asserts it stays at 0: that is
    what makes a removed lock or check FAIL the test even though the database
    backstop still protects the money."""
    with db.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM journal WHERE reason_code = 'INTEGRITY_BACKSTOP'").fetchone()["n"]


def run_proof() -> dict:
    with db.connect() as conn, conn.transaction():
        cur = conn.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        return proof.run_proof(cur)


def assert_books_balance() -> None:
    """Every money invariant holds. (The 'backstop' check is about application
    bugs, not balances; race tests assert on backstops() separately.)"""
    report = run_proof()
    failed = [(c["id"], c["problems"][:3]) for c in report["checks"] if not c["ok"] and c["id"] != "backstop"]
    assert not failed, failed


def all_at_once(fns) -> list:
    """Start every callable at the same instant (barrier) and collect results or exceptions."""
    fns = list(fns)
    barrier = threading.Barrier(len(fns))

    def call(fn):
        barrier.wait()
        try:
            return fn()
        except Exception as exc:          # returned, so the test can assert on it
            return exc

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        return list(pool.map(call, fns))


def http_login(base_url: str, username: str, password: str = PASSWORD) -> dict:
    r = httpx.post(f"{base_url}/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}
