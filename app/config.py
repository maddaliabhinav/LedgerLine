"""Settings, read once from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DEFAULT_SECRET = "dev-only-change-me"


def _load_dotenv() -> None:
    """Minimal .env loader (no extra dependency). Real env vars always win."""
    path = Path(os.environ.get("LEDGERLINE_ENV_FILE", ".env"))
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    app_env: str = "development"
    secret_key: str = DEFAULT_SECRET
    signing_key: str = ""
    access_token_minutes: int = 60
    db_pool_max: int = 20
    lock_timeout_ms: int = 5000
    statement_timeout_ms: int = 15000
    test_hooks: bool = False
    password_hash_n: int = 2**14
    anthropic_api_key: str = ""
    ai_model: str = "claude-haiku-5-5"
    ai_timeout_seconds: float = 12.0
    login_max_failures: int = 10
    login_window_seconds: int = 300
    signup_max: int = 10
    signup_window_seconds: int = 600
    trust_proxy: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv()
    url = os.environ.get("DATABASE_URL", "postgresql://ledgerline:ledgerline@localhost:5432/ledgerline")
    settings = Settings(
        database_url=url,
        app_env=os.environ.get("APP_ENV", "development").strip().lower(),
        secret_key=os.environ.get("SECRET_KEY", DEFAULT_SECRET),
        signing_key=os.environ.get("LEDGER_SIGNING_KEY", ""),
        access_token_minutes=int(os.environ.get("ACCESS_TOKEN_MINUTES", "60")),
        db_pool_max=int(os.environ.get("DB_POOL_MAX", "20")),
        lock_timeout_ms=int(os.environ.get("LOCK_TIMEOUT_MS", "5000")),
        statement_timeout_ms=int(os.environ.get("STATEMENT_TIMEOUT_MS", "15000")),
        test_hooks=_bool("LEDGER_TEST_HOOKS"),
        password_hash_n=int(os.environ.get("PASSWORD_HASH_N", str(2**14))),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        ai_model=os.environ.get("AI_MODEL", "claude-haiku-5-5").strip(),
        ai_timeout_seconds=float(os.environ.get("AI_TIMEOUT_SECONDS", "12")),
        trust_proxy=_bool("TRUST_PROXY", os.environ.get("APP_ENV", "").strip().lower() == "production"),
        signup_max=int(os.environ.get("SIGNUP_MAX", "10")),
        signup_window_seconds=int(os.environ.get("SIGNUP_WINDOW_SECONDS", "600")),
    )
    if settings.is_production:
        if settings.secret_key == DEFAULT_SECRET or len(settings.secret_key) < 32:
            raise RuntimeError("SECRET_KEY must be set to a long random value in production")
        if settings.test_hooks:
            raise RuntimeError("LEDGER_TEST_HOOKS must never be enabled in production")
    return settings
