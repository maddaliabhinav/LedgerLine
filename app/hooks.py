"""Test-only fault injection and race windows.

Concurrency tests must *prove* that locks matter. To make races deterministic,
the money code calls ``pause_point(name)`` at the dangerous spots (e.g. just
after the balance check). Tests can make a pause point sleep, so that without
the row lock two requests would both pass the check. ``fault_point(name)``
can raise, to simulate a request failing halfway.

Everything here is a no-op unless LEDGER_TEST_HOOKS=1, which config.py refuses
to allow in production.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator

from .config import get_settings


class InjectedFault(RuntimeError):
    """Raised by fault_point() when a test armed it."""


_lock = threading.Lock()
_delays: dict[str, float] = {}
_faults: set[str] = set()
_backend_pids: dict[str, list[int]] = {}


def enabled() -> bool:
    return get_settings().test_hooks


def pause_point(name: str, conn=None) -> None:
    if not enabled():
        return
    with _lock:
        delay = _delays.get(name)
        if conn is not None and name in _backend_pids:
            _backend_pids[name].append(conn.info.backend_pid)
    if delay:
        time.sleep(delay)


def fault_point(name: str) -> None:
    if not enabled():
        return
    with _lock:
        armed = name in _faults
    if armed:
        raise InjectedFault(f"injected fault at {name}")


@contextmanager
def race_window(**delays: float) -> Iterator[None]:
    """with race_window(after_balance_check=0.3): ... (test helper)"""
    with _lock:
        _delays.update(delays)
    try:
        yield
    finally:
        with _lock:
            for name in delays:
                _delays.pop(name, None)


@contextmanager
def faults(*names: str) -> Iterator[None]:
    with _lock:
        _faults.update(names)
    try:
        yield
    finally:
        with _lock:
            _faults.difference_update(names)


@contextmanager
def watch_backend(name: str) -> Iterator[list[int]]:
    """Collect the PostgreSQL backend pid of connections that reach a pause point."""
    pids: list[int] = []
    with _lock:
        _backend_pids[name] = pids
    try:
        yield pids
    finally:
        with _lock:
            _backend_pids.pop(name, None)
