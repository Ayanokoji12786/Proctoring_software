"""Schema-level integrity guarantees: UTC round-trip, FK enforcement,
unique enrollment, and index coverage."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def test_datetimes_round_trip_as_utc_aware():
    """Regression: SQLite drops tzinfo, so stored timestamps came back naive.
    Anything comparing them to an aware `now` raised TypeError, and anything
    serializing them emitted an offset-less string that JS reads as LOCAL time."""
    from database.db import session_scope
    from models.models import Event, ExamSession, Student

    with session_scope() as db:
        db.add(ExamSession(session_id="tz_exam", name="TZ"))
        db.add(Student(student_id="tz_student", display_name="TZ"))
        db.flush()
        db.add(
            Event(
                id="tz_event",
                session_id="tz_exam",
                student_id="tz_student",
                event_type="WINDOW_FOCUS_CHANGED",
                severity="yellow",
                timestamp=dt.datetime.now(dt.timezone.utc),
                metadata_json="{}",
            )
        )

    with session_scope() as db:
        row = db.get(Event, "tz_event")
        assert row.timestamp.tzinfo is not None, "timestamp must come back timezone-aware"
        assert row.timestamp.utcoffset() == dt.timedelta(0), "must be UTC"
        # The comparison that used to raise TypeError:
        assert (dt.datetime.now(dt.timezone.utc) - row.timestamp).total_seconds() >= 0


def test_api_serializes_timestamps_with_utc_offset(client, admin_headers):
    """A bare timestamp with no offset is parsed as local time by the dashboard."""
    resp = client.post("/api/sessions", json={"name": "Offset", "session_id": "tz_api"}, headers=admin_headers)
    created_at = resp.json()["created_at"]
    assert created_at.endswith("Z") or "+00:00" in created_at, f"no UTC offset in {created_at!r}"


def test_duplicate_enrollment_rows_are_rejected():
    """Regression: without a unique constraint, two concurrent enrollments both
    insert, and every later .one_or_none() raises MultipleResultsFound - locking
    that student out of the session permanently."""
    from database.db import session_scope
    from models.models import ExamSession, Student, StudentSession

    with session_scope() as db:
        db.add(ExamSession(session_id="uq_exam", name="UQ"))
        db.add(Student(student_id="uq_student", display_name="UQ"))
        db.flush()
        db.add(StudentSession(session_id="uq_exam", student_id="uq_student", token_jti="jti-1"))

    with pytest.raises(IntegrityError):
        with session_scope() as db:
            db.add(StudentSession(session_id="uq_exam", student_id="uq_student", token_jti="jti-2"))


def test_sqlite_foreign_keys_are_enforced():
    """Regression: SQLite ignores FK constraints unless the pragma is set, which
    made every ForeignKey in models.py decorative."""
    from database.db import session_scope
    from models.models import Event

    with session_scope() as db:
        assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1

    with pytest.raises(IntegrityError):
        with session_scope() as db:
            db.add(
                Event(
                    id="orphan_event",
                    session_id="session_that_does_not_exist",
                    student_id="student_that_does_not_exist",
                    event_type="WINDOW_FOCUS_CHANGED",
                    severity="yellow",
                    timestamp=dt.datetime.now(dt.timezone.utc),
                    metadata_json="{}",
                )
            )


def test_event_query_indexes_exist():
    """The dashboard's timeline/filter queries narrow by session/student/time."""
    from database.db import session_scope

    with session_scope() as db:
        names = {row[1] for row in db.execute(text("PRAGMA index_list('events')")).fetchall()}

    assert "ix_events_session_timestamp" in names
    assert "ix_events_session_student" in names
