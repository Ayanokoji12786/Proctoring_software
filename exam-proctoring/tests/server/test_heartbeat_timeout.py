"""Heartbeat timeout detection used by the server's watchdog task."""
from __future__ import annotations

import datetime as dt


def _add_state(manager, session_id, student_id, *, status, heartbeat_age_seconds):
    """Registers a state through the manager's real (session_id, student_id) key."""
    from websocket.manager import StudentState

    state = StudentState(student_id=student_id, display_name=student_id, session_id=session_id)
    state.status = status
    state.last_heartbeat = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=heartbeat_age_seconds)
    manager.states[(session_id, student_id)] = state
    return state


def test_offline_candidates_flags_stale_heartbeat():
    from websocket.manager import ConnectionManager

    manager = ConnectionManager()
    _add_state(manager, "exam_1", "stale_student", status="online", heartbeat_age_seconds=60)
    _add_state(manager, "exam_1", "fresh_student", status="online", heartbeat_age_seconds=0)
    _add_state(manager, "exam_1", "offline_student", status="offline", heartbeat_age_seconds=999)

    candidates = manager.offline_candidates(timeout_seconds=20)

    assert ("exam_1", "stale_student") in candidates
    assert ("exam_1", "fresh_student") not in candidates
    # already offline: not a *new* timeout, so it must not be re-reported
    assert ("exam_1", "offline_student") not in candidates


def test_offline_candidates_empty_when_all_fresh():
    from websocket.manager import ConnectionManager

    manager = ConnectionManager()
    _add_state(manager, "exam_1", "s1", status="online", heartbeat_age_seconds=0)

    assert manager.offline_candidates(timeout_seconds=20) == []


def test_offline_candidates_are_scoped_per_session():
    """The same student_id enrolled in two exams must be tracked independently -
    a lapse in one session must not mark the other offline."""
    from websocket.manager import ConnectionManager

    manager = ConnectionManager()
    _add_state(manager, "exam_a", "same_id", status="online", heartbeat_age_seconds=60)
    _add_state(manager, "exam_b", "same_id", status="online", heartbeat_age_seconds=0)

    candidates = manager.offline_candidates(timeout_seconds=20)

    assert candidates == [("exam_a", "same_id")]
