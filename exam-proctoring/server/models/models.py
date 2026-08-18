"""ORM models for the proctoring database.

Tables: students, exam_sessions, student_sessions, events, evidence
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.db import Base, UtcDateTime


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Student(Base):
    __tablename__ = "students"

    student_id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(timezone=True), default=_now)

    student_sessions: Mapped[list["StudentSession"]] = relationship(back_populates="student")


class ExamSession(Base):
    __tablename__ = "exam_sessions"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(timezone=True), default=_now)
    ended_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime(timezone=True), nullable=True)

    student_sessions: Mapped[list["StudentSession"]] = relationship(back_populates="exam_session")


class StudentSession(Base):
    """One student's enrollment/connection record within an exam session."""

    __tablename__ = "student_sessions"
    # A student is enrolled in a given exam session at most once. Without this,
    # two concurrent enrollment requests both see "no existing row" and each
    # insert one; every subsequent .one_or_none() lookup then raises
    # MultipleResultsFound, permanently locking that student out of the session.
    __table_args__ = (UniqueConstraint("session_id", "student_id", name="uq_student_session"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("exam_sessions.session_id"), nullable=False)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.student_id"), nullable=False)
    token_jti: Mapped[str] = mapped_column(String, unique=True, default=_uuid)
    consent_given: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, default="offline")  # online | offline
    joined_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime(timezone=True), nullable=True)
    left_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime(timezone=True), nullable=True)
    last_heartbeat: Mapped[dt.datetime | None] = mapped_column(UtcDateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(timezone=True), default=_now)

    student: Mapped["Student"] = relationship(back_populates="student_sessions")
    exam_session: Mapped["ExamSession"] = relationship(back_populates="student_sessions")


class Event(Base):
    __tablename__ = "events"
    # The dashboard's timeline/filter queries all narrow by session (usually
    # ordered by time) and by student; without these every query is a full
    # table scan that grows with the whole event history.
    __table_args__ = (
        Index("ix_events_session_timestamp", "session_id", "timestamp"),
        Index("ix_events_session_student", "session_id", "student_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)  # == event_id
    session_id: Mapped[str] = mapped_column(ForeignKey("exam_sessions.session_id"), nullable=False)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.student_id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)  # green | yellow | red
    timestamp: Mapped[dt.datetime] = mapped_column(UtcDateTime(timezone=True), nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    evidence_id: Mapped[str | None] = mapped_column(ForeignKey("evidence.id"), nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(timezone=True), default=_now)


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_session_student", "session_id", "student_id"),
        # The retention purge sweeps on (expires_at, deleted) on a timer.
        Index("ix_evidence_expiry", "expires_at", "deleted"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    student_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    captured_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(timezone=True), default=_now)
    expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime(timezone=True), nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
