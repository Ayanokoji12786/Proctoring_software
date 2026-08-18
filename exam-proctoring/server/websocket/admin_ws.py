"""Admin Dashboard WebSocket endpoint: pushes live student status + proctor events.

Auth uses a first-message AUTH frame rather than a query parameter. A
`?api_key=...` query string is written verbatim into web-server access logs
(uvicorn logs the full path on every connection), browser history, and any
intermediate proxy log - i.e. the admin credential ends up in plaintext in
several places nobody thinks to protect. Browsers can't set custom headers on
a WebSocket handshake, so the first-frame handshake (the same pattern the
student endpoint already uses) is the practical alternative.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from auth.tokens import verify_admin_api_key
from database.db import session_scope
from models.models import ExamSession
from websocket.manager import manager

logger = logging.getLogger("proctoring.websocket.admin")
router = APIRouter()

AUTH_TIMEOUT_SECONDS = 10


async def _authenticate(websocket: WebSocket) -> str | None:
    """Waits for the opening AUTH frame; returns the session_id to watch."""
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.send_json({"type": "AUTH_FAILED", "reason": "malformed JSON"})
        return None

    if data.get("type") != "AUTH":
        await websocket.send_json({"type": "AUTH_FAILED", "reason": "first message must be AUTH"})
        return None

    if not verify_admin_api_key(data.get("api_key", "")):
        logger.warning("admin websocket rejected: invalid API key")
        await websocket.send_json({"type": "AUTH_FAILED", "reason": "invalid admin API key"})
        return None

    session_id = data.get("session_id", "")
    if not session_id:
        await websocket.send_json({"type": "AUTH_FAILED", "reason": "session_id is required"})
        return None

    with session_scope() as db:
        if db.get(ExamSession, session_id) is None:
            await websocket.send_json({"type": "AUTH_FAILED", "reason": "exam session not found"})
            return None

    await websocket.send_json({"type": "AUTH_OK", "session_id": session_id})
    return session_id


@router.websocket("/ws/admin")
async def admin_websocket(websocket: WebSocket) -> None:
    await websocket.accept()

    session_id = await _authenticate(websocket)
    if session_id is None:
        await websocket.close(code=4401)
        return

    await manager.connect_admin(websocket, session_id)
    await websocket.send_json(manager.snapshot(session_id))
    logger.info("admin dashboard connected session_id=%s", session_id)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if data.get("type") == "REQUEST_SNAPSHOT":
                await websocket.send_json(manager.snapshot(session_id))
            elif data.get("type") == "ACK_STUDENT":
                student_id = data.get("student_id")
                if student_id:
                    # Scoped to this dashboard's own session, so one proctor
                    # can't clear alerts for a student in someone else's exam.
                    manager.reset_student_severity(student_id, session_id)
                    state = manager.get_state(student_id, session_id)
                    if state:
                        await manager.broadcast_status(state)
            # PING/keepalive frames from the dashboard are silently accepted

    except WebSocketDisconnect:
        logger.info("admin dashboard disconnected session_id=%s", session_id)
    finally:
        manager.disconnect_admin(websocket)
