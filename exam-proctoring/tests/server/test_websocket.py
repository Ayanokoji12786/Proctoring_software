"""WebSocket protocol: auth handshake, duplicate suppression, heartbeat, admin broadcast."""
from __future__ import annotations

import datetime as dt


def _create_session_and_student(client, admin_headers, session_id, student_id, name="Student"):
    client.post("/api/sessions", json={"name": session_id, "session_id": session_id}, headers=admin_headers)
    resp = client.post(
        "/api/sessions/{}/students".format(session_id),
        json={"session_id": session_id, "student_id": student_id, "display_name": name},
        headers=admin_headers,
    )
    return resp.json()["token"]


def _event_payload(session_id, student_id, event_id, event_type="WINDOW_FOCUS_CHANGED", severity="yellow"):
    return {
        "type": "EVENT",
        "payload": {
            "event_id": event_id,
            "student_id": student_id,
            "session_id": session_id,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "event_type": event_type,
            "severity": severity,
            "metadata": {},
        },
    }


def test_auth_success(client, admin_headers):
    token = _create_session_and_student(client, admin_headers, "exam_ws_1", "s1")
    with client.websocket_connect("/ws/student") as ws:
        ws.send_json({"type": "AUTH", "session_id": "exam_ws_1", "student_id": "s1", "token": token})
        resp = ws.receive_json()
        assert resp == {"type": "AUTH_OK", "session_id": "exam_ws_1", "student_id": "s1"}


def test_auth_failure_wrong_token(client, admin_headers):
    _create_session_and_student(client, admin_headers, "exam_ws_2", "s2")
    with client.websocket_connect("/ws/student") as ws:
        ws.send_json({"type": "AUTH", "session_id": "exam_ws_2", "student_id": "s2", "token": "not-a-real-token"})
        resp = ws.receive_json()
        assert resp["type"] == "AUTH_FAILED"


def test_auth_failure_unknown_session(client, admin_headers):
    with client.websocket_connect("/ws/student") as ws:
        ws.send_json({"type": "AUTH", "session_id": "does_not_exist", "student_id": "nobody", "token": "x"})
        resp = ws.receive_json()
        assert resp["type"] == "AUTH_FAILED"
        # Deliberately generic: an unauthenticated peer must not be able to
        # tell "session not found" apart from "bad token" or "not enrolled" -
        # that distinction previously let anyone enumerate valid session/
        # student IDs with zero valid credentials.
        assert resp["reason"] == "authentication failed"


def test_duplicate_event_suppressed(client, admin_headers):
    token = _create_session_and_student(client, admin_headers, "exam_ws_3", "s3")
    with client.websocket_connect("/ws/student") as ws:
        ws.send_json({"type": "AUTH", "session_id": "exam_ws_3", "student_id": "s3", "token": token})
        ws.receive_json()

        event_id = "22222222-2222-2222-2222-222222222222"
        ws.send_json(_event_payload("exam_ws_3", "s3", event_id))
        first_ack = ws.receive_json()
        assert first_ack["duplicate"] is False

        ws.send_json(_event_payload("exam_ws_3", "s3", event_id))
        second_ack = ws.receive_json()
        assert second_ack["duplicate"] is True

    resp = client.get("/api/events", params={"session_id": "exam_ws_3"}, headers=admin_headers)
    matching = [e for e in resp.json() if e["event_id"] == event_id]
    assert len(matching) == 1  # never double-persisted despite being sent twice


def test_heartbeat_ack(client, admin_headers):
    token = _create_session_and_student(client, admin_headers, "exam_ws_4", "s4")
    with client.websocket_connect("/ws/student") as ws:
        ws.send_json({"type": "AUTH", "session_id": "exam_ws_4", "student_id": "s4", "token": token})
        ws.receive_json()
        ws.send_json({"type": "HEARTBEAT", "student_id": "s4", "timestamp": dt.datetime.now(dt.timezone.utc).isoformat()})
        resp = ws.receive_json()
        assert resp["type"] == "HEARTBEAT_ACK"


def test_event_rejected_for_student_session_mismatch(client, admin_headers):
    token = _create_session_and_student(client, admin_headers, "exam_ws_5", "s5")
    with client.websocket_connect("/ws/student") as ws:
        ws.send_json({"type": "AUTH", "session_id": "exam_ws_5", "student_id": "s5", "token": token})
        ws.receive_json()
        # payload claims a different student_id than the authenticated one
        bad_payload = _event_payload("exam_ws_5", "someone_else", "33333333-3333-3333-3333-333333333333")
        ws.send_json(bad_payload)
        resp = ws.receive_json()
        assert resp["type"] == "ERROR"


def _admin_connect(client, session_id, api_key="test-admin-key"):
    """Opens an authenticated admin socket and returns it past the AUTH handshake."""
    ws = client.websocket_connect("/ws/admin").__enter__()
    ws.send_json({"type": "AUTH", "api_key": api_key, "session_id": session_id})
    return ws


def test_admin_receives_snapshot_and_live_broadcast(client, admin_headers):
    token = _create_session_and_student(client, admin_headers, "exam_ws_6", "s6", name="Broadcast Student")

    with client.websocket_connect("/ws/admin") as admin_ws:
        admin_ws.send_json({"type": "AUTH", "api_key": "test-admin-key", "session_id": "exam_ws_6"})
        assert admin_ws.receive_json()["type"] == "AUTH_OK"
        snapshot = admin_ws.receive_json()
        assert snapshot["type"] == "SNAPSHOT"

        with client.websocket_connect("/ws/student") as student_ws:
            student_ws.send_json({"type": "AUTH", "session_id": "exam_ws_6", "student_id": "s6", "token": token})
            student_ws.receive_json()

            status_msg = admin_ws.receive_json()
            assert status_msg["type"] == "STUDENT_STATUS"
            assert status_msg["student_id"] == "s6"
            assert status_msg["status"] == "online"

            event_id = "44444444-4444-4444-4444-444444444444"
            student_ws.send_json(_event_payload("exam_ws_6", "s6", event_id, event_type="MULTIPLE_FACES_DETECTED", severity="red"))
            student_ws.receive_json()  # ack

            proctor_event = admin_ws.receive_json()
            assert proctor_event["type"] == "PROCTOR_EVENT"
            assert proctor_event["payload"]["event_id"] == event_id

            status_update = admin_ws.receive_json()
            assert status_update["type"] == "STUDENT_STATUS"
            assert status_update["overall_severity"] == "red"
            assert status_update["alert_count"] == 1


def test_admin_websocket_rejects_bad_api_key(client, admin_headers):
    _create_session_and_student(client, admin_headers, "exam_ws_7", "s7")
    with client.websocket_connect("/ws/admin") as ws:
        ws.send_json({"type": "AUTH", "api_key": "wrong-key", "session_id": "exam_ws_7"})
        resp = ws.receive_json()
        assert resp["type"] == "AUTH_FAILED"
        assert "invalid admin API key" in resp["reason"]


def test_admin_websocket_rejects_unknown_session(client):
    with client.websocket_connect("/ws/admin") as ws:
        ws.send_json({"type": "AUTH", "api_key": "test-admin-key", "session_id": "no_such_exam"})
        resp = ws.receive_json()
        assert resp["type"] == "AUTH_FAILED"
        assert "not found" in resp["reason"]


def test_admin_only_receives_events_for_its_own_session(client, admin_headers):
    """Regression: a proctor watching one exam must never receive another exam's
    students or events (the manager previously broadcast globally)."""
    _create_session_and_student(client, admin_headers, "exam_iso_a", "iso_a", name="Alice")
    token_b = _create_session_and_student(client, admin_headers, "exam_iso_b", "iso_b", name="Bob")

    with client.websocket_connect("/ws/admin") as admin_a:
        admin_a.send_json({"type": "AUTH", "api_key": "test-admin-key", "session_id": "exam_iso_a"})
        assert admin_a.receive_json()["type"] == "AUTH_OK"
        assert admin_a.receive_json()["type"] == "SNAPSHOT"

        # A student in the *other* exam connects and emits a high-severity event
        with client.websocket_connect("/ws/student") as student_b:
            student_b.send_json({"type": "AUTH", "session_id": "exam_iso_b", "student_id": "iso_b", "token": token_b})
            student_b.receive_json()
            student_b.send_json(
                _event_payload("exam_iso_b", "iso_b", "99999999-9999-9999-9999-999999999999", severity="red")
            )
            student_b.receive_json()  # ack

        # Nothing about exam_iso_b may reach the exam_iso_a dashboard.
        with client.websocket_connect("/ws/admin") as probe:
            probe.send_json({"type": "AUTH", "api_key": "test-admin-key", "session_id": "exam_iso_a"})
            probe.receive_json()
            snapshot = probe.receive_json()

        assert all(s["session_id"] == "exam_iso_a" for s in snapshot["students"])
        assert all(e["session_id"] == "exam_iso_a" for e in snapshot["recent_events"])


def test_superseded_token_is_rejected(client, admin_headers):
    """Regression: re-enrolling a student mints a new jti, which must invalidate
    the previously issued token instead of leaving both valid."""
    old_token = _create_session_and_student(client, admin_headers, "exam_revoke", "rev1")
    new_token = client.post(
        "/api/sessions/exam_revoke/students",
        json={"session_id": "exam_revoke", "student_id": "rev1", "display_name": "Student"},
        headers=admin_headers,
    ).json()["token"]
    assert old_token != new_token

    with client.websocket_connect("/ws/student") as ws:
        ws.send_json({"type": "AUTH", "session_id": "exam_revoke", "student_id": "rev1", "token": new_token})
        assert ws.receive_json()["type"] == "AUTH_OK"

    with client.websocket_connect("/ws/student") as ws:
        ws.send_json({"type": "AUTH", "session_id": "exam_revoke", "student_id": "rev1", "token": old_token})
        resp = ws.receive_json()
        assert resp["type"] == "AUTH_FAILED"
        # Generic on the wire (see test_auth_failure_unknown_session); the
        # specific "superseded" reason is only logged server-side now.
        assert resp["reason"] == "authentication failed"


def test_ending_a_session_disconnects_connected_agents(client, admin_headers):
    """Monitoring must actually stop when the exam ends - refusing only *new*
    connections would leave already-connected agents streaming indefinitely."""
    from websocket.manager import manager

    token = _create_session_and_student(client, admin_headers, "exam_endclose", "ec1")

    with client.websocket_connect("/ws/student") as ws:
        ws.send_json({"type": "AUTH", "session_id": "exam_endclose", "student_id": "ec1", "token": token})
        assert ws.receive_json()["type"] == "AUTH_OK"
        assert ("exam_endclose", "ec1") in manager.student_sockets

        resp = client.post("/api/sessions/exam_endclose/end", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["agents_disconnected"] == 1

        assert ws.receive_json()["type"] == "SESSION_ENDED"

    # In-memory state for the finished session is released, not retained forever.
    assert ("exam_endclose", "ec1") not in manager.student_sockets
    assert manager.get_state("ec1", "exam_endclose") is None


def test_cannot_connect_to_an_ended_session(client, admin_headers):
    token = _create_session_and_student(client, admin_headers, "exam_ended2", "ee1")
    client.post("/api/sessions/exam_ended2/end", headers=admin_headers)

    with client.websocket_connect("/ws/student") as ws:
        ws.send_json({"type": "AUTH", "session_id": "exam_ended2", "student_id": "ee1", "token": token})
        resp = ws.receive_json()
        assert resp["type"] == "AUTH_FAILED"
        # Generic on the wire (see test_auth_failure_unknown_session).
        assert resp["reason"] == "authentication failed"


def test_resent_event_is_not_double_counted_after_cache_loss(client, admin_headers):
    """Regression: dedup must be DB-backed. An in-memory-only cache is lost on
    restart, so a resent unacked event would be re-broadcast and counted twice."""
    from websocket.manager import manager

    token = _create_session_and_student(client, admin_headers, "exam_dedup", "dd1")
    event_id = "77777777-7777-7777-7777-777777777777"

    with client.websocket_connect("/ws/student") as ws:
        ws.send_json({"type": "AUTH", "session_id": "exam_dedup", "student_id": "dd1", "token": token})
        ws.receive_json()
        ws.send_json(_event_payload("exam_dedup", "dd1", event_id, severity="red"))
        assert ws.receive_json()["duplicate"] is False

    state = manager.get_state("dd1", "exam_dedup")
    count_after_first = state.alert_count

    # Reconnect and resend the same event, as the agent does for unacked items.
    with client.websocket_connect("/ws/student") as ws:
        ws.send_json({"type": "AUTH", "session_id": "exam_dedup", "student_id": "dd1", "token": token})
        ws.receive_json()
        ws.send_json(_event_payload("exam_dedup", "dd1", event_id, severity="red"))
        assert ws.receive_json()["duplicate"] is True

    assert manager.get_state("dd1", "exam_dedup").alert_count == count_after_first
    rows = client.get("/api/events", params={"session_id": "exam_dedup"}, headers=admin_headers).json()
    assert len([e for e in rows if e["event_id"] == event_id]) == 1
