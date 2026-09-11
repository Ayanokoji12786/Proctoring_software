"""Central configuration for the proctoring server, loaded from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    # Anchored to this file's directory, not the process's current working
    # directory - a relative "./.env" is resolved against whatever directory
    # the process happened to be launched from (e.g. a different WORKDIR in
    # Docker/systemd), which would silently skip loading .env entirely rather
    # than erroring, leaving the well-known CHANGE_ME_* defaults below active.
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore")

    # General
    app_name: str = "Exam Proctoring Server"
    environment: str = "development"

    # Database
    database_url: str = f"sqlite:///{BASE_DIR / 'proctoring.db'}"

    # Auth
    jwt_secret: str = "CHANGE_ME_DEV_SECRET_DO_NOT_USE_IN_PRODUCTION"
    jwt_algorithm: str = "HS256"
    student_token_ttl_minutes: int = 240  # short-lived, covers a typical exam window
    admin_api_key: str = "CHANGE_ME_ADMIN_KEY"

    # WebSocket / connection management
    heartbeat_interval_seconds: int = 5
    heartbeat_timeout_seconds: int = 20  # student marked offline if no heartbeat/event in this window

    # Evidence
    evidence_storage_dir: str = str(BASE_DIR / "evidence_storage")
    evidence_retention_hours: int = 72
    evidence_max_bytes: int = 2 * 1024 * 1024  # 2MB per snapshot
    # Plain comma-separated strings (not list/tuple types): pydantic-settings
    # requires JSON syntax in the env var for list/tuple fields, which is an
    # easy footgun in a hand-edited .env file. A plain string is what anyone
    # writing FOO=a,b,c in a .env file actually expects to work.
    evidence_allowed_content_types: str = "image/jpeg,image/png"

    # CORS (dashboard served separately during dev)
    cors_allow_origins: str = "*"

    @property
    def evidence_allowed_content_types_tuple(self) -> tuple[str, ...]:
        return tuple(t.strip() for t in self.evidence_allowed_content_types.split(",") if t.strip())

    @property
    def cors_allow_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]


settings = Settings()
os.makedirs(settings.evidence_storage_dir, exist_ok=True)

if settings.environment == "production":
    # These defaults are committed in plaintext in .env.example; running
    # production with either one still in place means anyone who has read
    # the public repo can forge a valid student JWT (jwt_secret) or supply
    # the known key for every admin-gated endpoint (admin_api_key). Fail
    # loudly at startup instead of silently accepting requests signed with a
    # secret the whole internet already has.
    if settings.jwt_secret == "CHANGE_ME_DEV_SECRET_DO_NOT_USE_IN_PRODUCTION":
        raise RuntimeError("ENVIRONMENT=production but JWT_SECRET is still the default placeholder - set a real secret")
    if settings.admin_api_key == "CHANGE_ME_ADMIN_KEY":
        raise RuntimeError("ENVIRONMENT=production but ADMIN_API_KEY is still the default placeholder - set a real key")
