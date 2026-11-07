"""PostgreSQL access: one connection pool, explicit transactions.

Connections run in autocommit mode and every unit of work opens an explicit
``with conn.transaction():`` block, so it is always obvious in the code where
a transaction begins and ends. A nested ``conn.transaction()`` is a SAVEPOINT.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

SCHEMA = "ledgerline"
_pool: ConnectionPool | None = None


def normalise_url(url: str) -> str:
    # Some hosts hand out postgres://; libpq accepts both, keep it explicit.
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def _configure(conn: psycopg.Connection) -> None:
    conn.autocommit = True
    conn.execute(f"SET search_path TO {SCHEMA}, public")
    conn.execute("SET TIME ZONE 'UTC'")


def connect() -> psycopg.Connection:
    """A standalone connection (CLI, tests, tools)."""
    conn = psycopg.connect(normalise_url(get_settings().database_url), row_factory=dict_row, autocommit=True)
    _configure(conn)
    return conn


def init_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = ConnectionPool(
            normalise_url(settings.database_url),
            min_size=1,
            max_size=settings.db_pool_max,
            kwargs={"row_factory": dict_row, "autocommit": True},
            configure=_configure,
            open=True,
            timeout=30,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with init_pool().connection() as conn:
        yield conn


@contextmanager
def read_transaction(snapshot: bool = False) -> Iterator[psycopg.Cursor]:
    """Read-only transaction. snapshot=True gives one consistent REPEATABLE READ view."""
    with connection() as conn:
        with conn.transaction():
            cur = conn.cursor()
            if snapshot:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            else:
                cur.execute("SET TRANSACTION READ ONLY")
            yield cur
