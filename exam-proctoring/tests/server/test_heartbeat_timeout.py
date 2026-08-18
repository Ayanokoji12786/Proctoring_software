"""Heartbeat timeout detection used by the server's watchdog task."""
from __future__ import annotations

import datetime as dt


def test_offline_candidates_flags_stale_heartbeat():
    from websocket.manager import ConnectionManager, StudentState

    manager = ConnectionManager()
    stale = StudentState(student_id="stale_student", display_name="Stale", session_id="exam_1")
    stale.status = "online"
    stale.last_heartbeat = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)
    manager.states["stale_student"] = stale

    fresh = StudentState(student_id="fresh_student", display_name="Fresh", session_id="exam_1")
    fresh.status = "online"
    fresh.last_heartbeat = dt.datetime.now(dt.timezone.utc)
    manager.states["fresh_student"] = fresh

    already_offline = StudentState(student_id="offline_student", display_name="Offline", session_id="exam_1")
    already_offline.status = "offline"
    already_offline.last_heartbeat = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=999)
    manager.states["offline_student"] = already_offline

    candidates = manager.offline_candidates(timeout_seconds=20)

    assert "stale_student" in candidates
    assert "fresh_student" not in candidates
    assert "offline_student" not in candidates  # already offline, not a new timeout


def test_offline_candidates_empty_when_all_fresh():
    from websocket.manager import ConnectionManager, StudentState

    manager = ConnectionManager()
    state = StudentState(student_id="s1", display_name="S1", session_id="exam_1")
    state.status = "online"
    state.last_heartbeat = dt.datetime.now(dt.timezone.utc)
    manager.states["s1"] = state

    assert manager.offline_candidates(timeout_seconds=20) == []
