"""ConnectionManager: session isolation and reconnect-race handling."""
from __future__ import annotations

import datetime as dt

import pytest


class FakeWS:
    def __init__(self, label="ws"):
        self.label = label
        self.received = []

    async def send_json(self, message, mode="text"):
        self.received.append(message)


def _event(session_id, student_id, event_id="e1", severity="red"):
    return {
        "event_id": event_id,
        "student_id": student_id,
        "session_id": session_id,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "event_type": "MULTIPLE_FACES_DETECTED",
        "severity": severity,
        "metadata": {},
    }


@pytest.mark.asyncio
async def test_events_are_not_broadcast_across_sessions():
    """Regression: a proctor watching exam A previously received exam B's events."""
    from websocket.manager import ConnectionManager

    manager = ConnectionManager()
    admin_a, admin_b = FakeWS("a"), FakeWS("b")
    await manager.connect_admin(admin_a, "exam_a")
    await manager.connect_admin(admin_b, "exam_b")

    await manager.record_event(_event("exam_b", "student_b"))

    assert admin_a.received == []
    assert [m["type"] for m in admin_b.received] == ["PROCTOR_EVENT", "STUDENT_STATUS"]


@pytest.mark.asyncio
async def test_snapshot_only_contains_requested_session():
    from websocket.manager import ConnectionManager

    manager = ConnectionManager()
    await manager.record_event(_event("exam_a", "student_a", event_id="ea"))
    await manager.record_event(_event("exam_b", "student_b", event_id="eb"))

    snap_a = manager.snapshot("exam_a")

    assert [s["student_id"] for s in snap_a["students"]] == ["student_a"]
    assert [e["session_id"] for e in snap_a["recent_events"]] == ["exam_a"]


@pytest.mark.asyncio
async def test_same_student_id_in_two_sessions_tracked_independently():
    from websocket.manager import ConnectionManager

    manager = ConnectionManager()
    await manager.connect_student("same_id", "Alex", "exam_a", FakeWS())
    await manager.connect_student("same_id", "Alex", "exam_b", FakeWS())

    await manager.record_event(_event("exam_a", "same_id", event_id="x1"))

    assert manager.get_state("same_id", "exam_a").alert_count == 1
    assert manager.get_state("same_id", "exam_b").alert_count == 0


@pytest.mark.asyncio
async def test_stale_disconnect_does_not_mark_reconnected_student_offline():
    """Regression: on a fast reconnect the new socket registers before the old
    connection's cleanup runs; that late cleanup must not evict the new socket."""
    from websocket.manager import ConnectionManager

    manager = ConnectionManager()
    old_conn, new_conn = FakeWS("old"), FakeWS("new")

    await manager.connect_student("st1", "Alex", "exam_a", old_conn)
    await manager.connect_student("st1", "Alex", "exam_a", new_conn)

    # The dropped connection's finally-block cleanup fires late.
    await manager.disconnect_student("st1", "exam_a", old_conn)

    state = manager.get_state("st1", "exam_a")
    assert state.status == "online", "student is still connected via the newer socket"
    assert manager.student_sockets[("exam_a", "st1")] is new_conn


@pytest.mark.asyncio
async def test_current_connection_disconnect_does_mark_offline():
    from websocket.manager import ConnectionManager

    manager = ConnectionManager()
    conn = FakeWS()
    await manager.connect_student("st1", "Alex", "exam_a", conn)
    await manager.disconnect_student("st1", "exam_a", conn)

    assert manager.get_state("st1", "exam_a").status == "offline"
    assert ("exam_a", "st1") not in manager.student_sockets


@pytest.mark.asyncio
async def test_ack_student_is_scoped_to_session():
    from websocket.manager import ConnectionManager

    manager = ConnectionManager()
    await manager.record_event(_event("exam_a", "same_id", event_id="a1"))
    await manager.record_event(_event("exam_b", "same_id", event_id="b1"))

    manager.reset_student_severity("same_id", "exam_a")

    assert manager.get_state("same_id", "exam_a").alert_count == 0
    assert manager.get_state("same_id", "exam_b").alert_count == 1, "other session must be untouched"
