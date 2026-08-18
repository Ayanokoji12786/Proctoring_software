"""Database persistence: sessions, enrollment, events survive a round trip through the ORM."""
from __future__ import annotations

import datetime as dt
import json


def test_session_and_enrollment_persist(client, admin_headers):
    resp = client.post("/api/sessions", json={"name": "DB Test", "session_id": "exam_db_test"}, headers=admin_headers)
    assert resp.status_code == 200

    resp = client.post(
        "/api/sessions/exam_db_test/students",
        json={"session_id": "exam_db_test", "student_id": "db_student_1", "display_name": "DB Student"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["student_id"] == "db_student_1"
    assert "token" in body

    resp = client.get("/api/sessions/exam_db_test/students", headers=admin_headers)
    assert resp.status_code == 200
    students = resp.json()
    assert any(s["student_id"] == "db_student_1" for s in students)


def test_event_persists_and_is_queryable(client, admin_headers):
    from database.db import session_scope
    from models.models import Event

    client.post("/api/sessions", json={"name": "Event DB Test", "session_id": "exam_event_db"}, headers=admin_headers)
    enroll = client.post(
        "/api/sessions/exam_event_db/students",
        json={"session_id": "exam_event_db", "student_id": "student_evt", "display_name": "Evt Student"},
        headers=admin_headers,
    ).json()
    token = enroll["token"]

    with client.websocket_connect("/ws/student") as ws:
        ws.send_json({"type": "AUTH", "session_id": "exam_event_db", "student_id": "student_evt", "token": token})
        assert ws.receive_json()["type"] == "AUTH_OK"

        event_id = "11111111-1111-1111-1111-111111111111"
        ws.send_json(
            {
                "type": "EVENT",
                "payload": {
                    "event_id": event_id,
                    "student_id": "student_evt",
                    "session_id": "exam_event_db",
                    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "event_type": "WINDOW_FOCUS_CHANGED",
                    "severity": "yellow",
                    "metadata": {"previous_application": "A", "current_application": "B"},
                },
            }
        )
        ack = ws.receive_json()
        assert ack == {"type": "EVENT_ACK", "event_id": event_id, "duplicate": False}

    with session_scope() as db:
        row = db.get(Event, event_id)
        assert row is not None
        assert row.event_type == "WINDOW_FOCUS_CHANGED"
        assert json.loads(row.metadata_json)["current_application"] == "B"

    resp = client.get("/api/events", params={"session_id": "exam_event_db"}, headers=admin_headers)
    assert resp.status_code == 200
    events = resp.json()
    assert any(e["event_id"] == event_id for e in events)
