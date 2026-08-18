"""Admin Dashboard WebSocket endpoint: pushes live student status + proctor events."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from auth.tokens import verify_admin_api_key
from websocket.manager import manager

logger = logging.getLogger("proctoring.websocket.admin")
router = APIRouter()


@router.websocket("/ws/admin")
async def admin_websocket(websocket: WebSocket) -> None:
    api_key = websocket.query_params.get("api_key", "")
    if not verify_admin_api_key(api_key):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    await manager.connect_admin(websocket)
    await websocket.send_json(manager.snapshot())
    logger.info("admin dashboard connected")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if data.get("type") == "REQUEST_SNAPSHOT":
                await websocket.send_json(manager.snapshot())
            elif data.get("type") == "ACK_STUDENT":
                student_id = data.get("student_id")
                if student_id:
                    manager.reset_student_severity(student_id)
                    state = manager.states.get(student_id)
                    if state:
                        await manager.broadcast_status(state)
            # PING/keepalive frames from the dashboard are silently accepted

    except WebSocketDisconnect:
        logger.info("admin dashboard disconnected")
    finally:
        manager.disconnect_admin(websocket)
