"""SQLAlchemy engine/session setup.

Uses SQLite for the prototype. The models and session layer use standard
SQLAlchemy ORM constructs with no SQLite-specific types, so migrating to
PostgreSQL later only requires changing `database_url` (and adding the
`psycopg` driver) — no model or query changes are needed.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import DateTime, TypeDecorator, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


if settings.database_url.startswith("sqlite"):

    @event.listens_for(Engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):  # pragma: no cover - driver hook
        """SQLite ignores FOREIGN KEY constraints unless this pragma is set per
        connection, which silently turns every FK in models.py into decoration.
        PostgreSQL enforces them natively, so this is SQLite-only.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class UtcDateTime(TypeDecorator):
    """A DateTime that always round-trips as timezone-aware UTC.

    SQLite has no native timestamp type and discards tzinfo, so an aware
    datetime written here comes back naive. Anything that then compares it
    against an aware "now" raises TypeError, and anything that serializes it
    to JSON emits a bare timestamp with no offset - which JavaScript's
    `new Date(...)` interprets as *local* time, silently shifting every
    timestamp in the dashboard by the viewer's UTC offset.

    This normalizes both directions: naive values are assumed to be UTC (they
    always are here), and loaded values are re-tagged as UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: dt.datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from models import models  # noqa: F401  (ensure models are registered)

    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Context manager for use outside of FastAPI request scope (e.g. background tasks)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
