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
        assert "not found" in resp["reason"]


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


def test_admin_receives_snapshot_and_live_broadcast(client, admin_headers):
    token = _create_session_and_student(client, admin_headers, "exam_ws_6", "s6", name="Broadcast Student")

    with client.websocket_connect("/ws/admin?api_key=test-admin-key") as admin_ws:
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


def test_admin_websocket_rejects_bad_api_key(client):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/admin?api_key=wrong-key"):
            pass
