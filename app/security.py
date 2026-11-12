"""Passwords (scrypt, stdlib) and access tokens (JWT, HS256)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import jwt

from .config import get_settings
from .errors import TooManyRequests, Unauthorized

ROLES = ("admin", "holder", "auditor")
MIN_PASSWORD_LENGTH = 10


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    n, r, p = get_settings().password_hash_n, 8, 1
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32,
                            maxmem=256 * 1024 * 1024)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64),
                                n=int(n), r=int(r), p=int(p), dklen=32, maxmem=256 * 1024 * 1024)
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """Hash checked when the username does not exist, so timing does not reveal it."""
    return hash_password(secrets.token_urlsafe(16))


def create_access_token(user: dict) -> tuple[str, int]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    lifetime = timedelta(minutes=settings.access_token_minutes)
    claims = {
        "sub": str(user["id"]),
        "tv": user["token_version"],
        "role": user["role"],          # informational only: the server re-reads the role from the DB
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "jti": secrets.token_hex(8),
        "iss": "ledgerline",
    }
    token = jwt.encode(claims, settings.secret_key, algorithm="HS256")
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(
            token, get_settings().secret_key, algorithms=["HS256"],   # never "none", never RS/HS confusion
            options={"require": ["exp", "iat", "sub", "tv"]}, issuer="ledgerline")
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("access token expired", code="TOKEN_EXPIRED") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("invalid access token", code="TOKEN_INVALID") from exc


class LoginThrottle:
    """Failed-login limiter per client IP and per username (in-memory, per process).

    Limits online password guessing. It is deliberately keyed on the IP too, so an
    attacker cannot lock a real user out just by failing logins for their name.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, deque] = defaultdict(deque)

    def _prune(self, key: str, now: float, window: int) -> deque:
        q = self._failures[key]
        while q and now - q[0] > window:
            q.popleft()
        return q

    def check(self, ip: str, username: str, what: str = "sign-in attempts") -> None:
        settings = get_settings()
        now = time.monotonic()
        with self._lock:
            for key in (f"ip:{ip}", f"pair:{ip}:{username}"):
                if len(self._prune(key, now, settings.login_window_seconds)) >= settings.login_max_failures:
                    raise TooManyRequests(f"too many failed {what}; wait a few minutes",
                                          headers={"Retry-After": str(settings.login_window_seconds)})

    def fail(self, ip: str, username: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._failures[f"ip:{ip}"].append(now)
            self._failures[f"pair:{ip}:{username}"].append(now)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


login_throttle = LoginThrottle()


class SignUpThrottle:
    """Self-service sign-ups per client IP (in-memory, per process). Every
    attempt counts, successful or not, so one client cannot flood the system
    with users and accounts or probe usernames at speed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: dict[str, deque] = defaultdict(deque)

    def attempt(self, ip: str) -> None:
        settings = get_settings()
        now = time.monotonic()
        with self._lock:
            q = self._attempts[ip]
            while q and now - q[0] > settings.signup_window_seconds:
                q.popleft()
            if len(q) >= settings.signup_max:
                raise TooManyRequests("too many sign-ups from this address; try again later",
                                      headers={"Retry-After": str(settings.signup_window_seconds)})
            q.append(now)

    def reset(self) -> None:
        with self._lock:
            self._attempts.clear()


signup_throttle = SignUpThrottle()
