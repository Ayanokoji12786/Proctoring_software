"""Student Agent WebSocket endpoint: AUTH handshake, EVENT ingestion, HEARTBEAT."""
from __future__ import annotations

import datetime as dt
import json
import logging

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from auth.tokens import TokenError, verify_student_token, verify_token_not_superseded
from database.db import session_scope
from models.models import Event, ExamSession, Student, StudentSession
from schemas.schemas import EventPayload, Severity
from websocket.manager import manager

logger = logging.getLogger("proctoring.websocket.student")
router = APIRouter()

AUTH_TIMEOUT_SECONDS = 10


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _authenticate(websocket: WebSocket) -> tuple[str, str, str] | None:
    """Waits for the first AUTH frame and validates it. Returns (session_id, student_id, display_name)."""
    try:
        # Bounded wait: an unauthenticated peer that connects and then never
        # sends anything would otherwise hold the connection (and its server
        # resources) open indefinitely.
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("closing websocket: no AUTH frame within %ss", AUTH_TIMEOUT_SECONDS)
        return None
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

    # Every failure below returns the same generic reason to the client - an
    # unauthenticated peer presenting no valid token must not be able to
    # distinguish "session not found", "student not enrolled", "bad token",
    # and "token superseded" from each other, since the distinct messages
    # this used to send let anyone enumerate live session/student IDs with
    # zero valid credentials. The specific reason is still logged server-side
    # for diagnosability.
    generic_failure = {"type": "AUTH_FAILED", "reason": "authentication failed"}

    with session_scope() as db:  # type: Session
        exam_session = db.get(ExamSession, session_id)
        if exam_session is None or exam_session.ended_at is not None:
            logger.info("auth rejected (session not found/ended): session_id=%s", session_id)
            await websocket.send_json(generic_failure)
            return None

        student = db.get(Student, student_id)
        if student is None:
            logger.info("auth rejected (student not enrolled): student_id=%s", student_id)
            await websocket.send_json(generic_failure)
            return None

        try:
            claims = verify_student_token(token, session_id, student_id)
        except TokenError as exc:
            logger.info("auth rejected (%s): student_id=%s session_id=%s", exc, student_id, session_id)
            await websocket.send_json(generic_failure)
            return None

        student_session = (
            db.query(StudentSession)
            .filter_by(session_id=session_id, student_id=student_id)
            .one_or_none()
        )
        if student_session is None:
            logger.info("auth rejected (no enrollment row): student_id=%s session_id=%s", student_id, session_id)
            await websocket.send_json(generic_failure)
            return None

        try:
            verify_token_not_superseded(claims, student_session.token_jti)
        except TokenError as exc:
            logger.warning("rejected superseded token for student_id=%s session_id=%s", student_id, session_id)
            await websocket.send_json(generic_failure)
            return None

        student_session.status = "online"
        student_session.joined_at = student_session.joined_at or _now()
        student_session.last_heartbeat = _now()
        display_name = student.display_name

    await websocket.send_json({"type": "AUTH_OK", "session_id": session_id, "student_id": student_id})
    return session_id, student_id, display_name


def _persist_event(payload: EventPayload) -> bool:
    """Persists the event. Returns True if it was newly inserted, False if this
    event_id was already stored (i.e. the agent resent an unacknowledged event).

    The database - not an in-memory set - is the authority for deduplication.
    An in-process cache is lost on restart, so after a server restart an agent
    resending its unacked queue would be treated as brand new: the row insert
    would be skipped, but the event would still be re-broadcast to dashboards
    and counted again in the student's alert total. Returning the insert
    outcome lets the caller skip both of those for a known event_id.
    """
    with session_scope() as db:
        if db.get(Event, payload.event_id) is not None:
            return False
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
    return True


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

                try:
                    is_new = _persist_event(event_msg)
                except SQLAlchemyError:
                    # Don't drop the connection over one bad row - the agent
                    # keeps it queued and retries, and other events keep flowing.
                    logger.exception("failed to persist event %s", event_msg.event_id)
                    await websocket.send_json({"type": "ERROR", "reason": "event could not be stored; will retry"})
                    continue

                if is_new:
                    await manager.record_event(json.loads(event_msg.model_dump_json()))
                await websocket.send_json(
                    {"type": "EVENT_ACK", "event_id": event_msg.event_id, "duplicate": not is_new}
                )

            elif msg_type == "HEARTBEAT":
                manager.record_heartbeat(student_id, session_id)
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
        disconnected = await manager.disconnect_student(student_id, session_id, websocket)
        # Only persist "offline" if this really was the live connection going
        # away. On a fast reconnect, disconnect_student() ignores the stale
        # socket and returns False - unconditionally marking the DB offline
        # here anyway would overwrite a status a newer, still-connected
        # socket had just set back to "online", corrupting the attendance
        # record for a student who never actually left.
        if disconnected:
            _mark_offline(student_id, session_id)
