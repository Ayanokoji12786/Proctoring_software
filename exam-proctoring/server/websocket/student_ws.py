"""Student Agent WebSocket endpoint: AUTH handshake, EVENT ingestion, HEARTBEAT."""
from __future__ import annotations

import datetime as dt
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.orm import Session

from auth.tokens import TokenError, verify_student_token
from database.db import session_scope
from models.models import Event, ExamSession, Student, StudentSession
from schemas.schemas import EventPayload, Severity
from websocket.manager import manager

logger = logging.getLogger("proctoring.websocket.student")
router = APIRouter()

AUTH_TIMEOUT_SECONDS = 10
# Bound how many event_ids we remember per student for duplicate suppression
# after a reconnect (the agent resends its unacknowledged local queue).
_SEEN_EVENT_CACHE_SIZE = 500
_seen_event_ids: dict[str, set[str]] = {}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _mark_seen(student_id: str, event_id: str) -> bool:
    """Returns True if this event_id was already seen (i.e. it's a duplicate)."""
    seen = _seen_event_ids.setdefault(student_id, set())
    if event_id in seen:
        return True
    seen.add(event_id)
    if len(seen) > _SEEN_EVENT_CACHE_SIZE:
        # drop an arbitrary old entry to bound memory; good enough for a prototype
        seen.pop()
    return False


async def _authenticate(websocket: WebSocket) -> tuple[str, str, str] | None:
    """Waits for the first AUTH frame and validates it. Returns (session_id, student_id, display_name)."""
    try:
        raw = await websocket.receive_text()
    except WebSocketDisconnect:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.send_json({"type": "AUTH_FAILED", "reason": "malformed JSON"})
        return None

    if data.get("type") != "AUTH":
        await websocket.send_json({"type": "AUTH_FAILED", "reason": "first message must be AUTH"})
        return None

    session_id = data.get("session_id", "")
    student_id = data.get("student_id", "")
    token = data.get("token", "")

    with session_scope() as db:  # type: Session
        exam_session = db.get(ExamSession, session_id)
        if exam_session is None or exam_session.ended_at is not None:
            await websocket.send_json({"type": "AUTH_FAILED", "reason": "exam session not found or ended"})
            return None

        student = db.get(Student, student_id)
        if student is None:
            await websocket.send_json({"type": "AUTH_FAILED", "reason": "student not enrolled"})
            return None

        try:
            verify_student_token(token, session_id, student_id)
        except TokenError as exc:
            await websocket.send_json({"type": "AUTH_FAILED", "reason": str(exc)})
            return None

        student_session = (
            db.query(StudentSession)
            .filter_by(session_id=session_id, student_id=student_id)
            .one_or_none()
        )
        if student_session is None:
            await websocket.send_json({"type": "AUTH_FAILED", "reason": "student not enrolled in this session"})
            return None

        student_session.status = "online"
        student_session.joined_at = student_session.joined_at or _now()
        student_session.last_heartbeat = _now()
        display_name = student.display_name

    await websocket.send_json({"type": "AUTH_OK", "session_id": session_id, "student_id": student_id})
    return session_id, student_id, display_name


def _persist_event(payload: EventPayload) -> None:
    with session_scope() as db:
        exists = db.get(Event, payload.event_id)
        if exists is not None:
            return  # duplicate at the DB layer too (defense in depth)
        db.add(
            Event(
                id=payload.event_id,
                session_id=payload.session_id,
                student_id=payload.student_id,
                event_type=payload.event_type.value,
                severity=payload.severity.value,
                timestamp=payload.timestamp,
                metadata_json=json.dumps(payload.metadata),
                evidence_id=payload.evidence_id,
            )
        )


def _touch_heartbeat(student_id: str, session_id: str) -> None:
    with session_scope() as db:
        student_session = (
            db.query(StudentSession)
            .filter_by(session_id=session_id, student_id=student_id)
            .one_or_none()
        )
        if student_session:
            student_session.last_heartbeat = _now()
            student_session.status = "online"


def _mark_offline(student_id: str, session_id: str) -> None:
    with session_scope() as db:
        student_session = (
            db.query(StudentSession)
            .filter_by(session_id=session_id, student_id=student_id)
            .one_or_none()
        )
        if student_session:
            student_session.status = "offline"
            student_session.left_at = _now()


@router.websocket("/ws/student")
async def student_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    auth_result = await _authenticate(websocket)
    if auth_result is None:
        await websocket.close(code=4401)
        return

    session_id, student_id, display_name = auth_result
    await manager.connect_student(student_id, display_name, session_id, websocket)
    logger.info("student connected student_id=%s session_id=%s", student_id, session_id)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "ERROR", "reason": "malformed JSON"})
                continue

            msg_type = data.get("type")

            if msg_type == "EVENT":
                try:
                    event_msg = EventPayload.model_validate(data.get("payload", {}))
                except ValidationError as exc:
                    await websocket.send_json({"type": "ERROR", "reason": f"invalid event schema: {exc.error_count()} errors"})
                    continue

                if event_msg.student_id != student_id or event_msg.session_id != session_id:
                    await websocket.send_json({"type": "ERROR", "reason": "event student/session mismatch with authenticated identity"})
                    continue

                # clamp severity to a known enum value defensively (already validated by pydantic, kept for clarity)
                if event_msg.severity not in (Severity.GREEN, Severity.YELLOW, Severity.RED):
                    event_msg.severity = Severity.YELLOW

                if _mark_seen(student_id, event_msg.event_id):
                    await websocket.send_json({"type": "EVENT_ACK", "event_id": event_msg.event_id, "duplicate": True})
                    continue

                _persist_event(event_msg)
                event_dict = json.loads(event_msg.model_dump_json())
                await manager.record_event(event_dict)
                await websocket.send_json({"type": "EVENT_ACK", "event_id": event_msg.event_id, "duplicate": False})

            elif msg_type == "HEARTBEAT":
                manager.record_heartbeat(student_id)
                _touch_heartbeat(student_id, session_id)
                await websocket.send_json({"type": "HEARTBEAT_ACK", "timestamp": _now().isoformat()})

            elif msg_type == "CONSENT":
                logger.info("consent recorded student_id=%s categories=%s", student_id, data.get("categories"))
                with session_scope() as db:
                    student_session = (
                        db.query(StudentSession)
                        .filter_by(session_id=session_id, student_id=student_id)
                        .one_or_none()
                    )
                    if student_session:
                        student_session.consent_given = True

            elif msg_type == "END_EXAM":
                logger.info("student ended exam student_id=%s session_id=%s", student_id, session_id)
                break

            else:
                await websocket.send_json({"type": "ERROR", "reason": f"unknown message type: {msg_type}"})

    except WebSocketDisconnect:
        logger.info("student disconnected student_id=%s session_id=%s", student_id, session_id)
    finally:
        await manager.disconnect_student(student_id)
        _mark_offline(student_id, session_id)
