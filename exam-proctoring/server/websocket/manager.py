"""In-memory connection registry shared by the student and admin WebSocket endpoints.

This is the "Student Connections" + "Event Broadcasting" piece of the server
architecture: it tracks who is online, the last known state per student, and
fans out events to every connected admin dashboard in real time.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import WebSocket

from schemas.schemas import Severity

logger = logging.getLogger("proctoring.websocket.manager")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass
class StudentState:
    student_id: str
    display_name: str
    session_id: str
    status: str = "offline"  # online | offline
    current_state: str = "UNKNOWN"
    overall_severity: Severity = Severity.GREEN
    alert_count: int = 0
    last_event_type: Optional[str] = None
    last_event_at: Optional[dt.datetime] = None
    last_heartbeat: Optional[dt.datetime] = None
    connected_at: Optional[dt.datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "student_id": self.student_id,
            "display_name": self.display_name,
            "session_id": self.session_id,
            "status": self.status,
            "current_state": self.current_state,
            "overall_severity": self.overall_severity.value,
            "alert_count": self.alert_count,
            "last_event_type": self.last_event_type,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
        }


_SEVERITY_RANK = {Severity.GREEN: 0, Severity.YELLOW: 1, Severity.RED: 2}


class ConnectionManager:
    def __init__(self) -> None:
        self.student_sockets: dict[str, WebSocket] = {}
        self.admin_sockets: set[WebSocket] = set()
        self.states: dict[str, StudentState] = {}
        self.recent_events: list[dict[str, Any]] = []
        self._max_recent_events = 200

    # ---------------- student lifecycle ----------------

    async def connect_student(self, student_id: str, display_name: str, session_id: str, websocket: WebSocket) -> None:
        self.student_sockets[student_id] = websocket
        state = self.states.get(student_id)
        if state is None:
            state = StudentState(student_id=student_id, display_name=display_name, session_id=session_id)
            self.states[student_id] = state
        state.status = "online"
        state.session_id = session_id
        state.display_name = display_name
        state.connected_at = _now()
        state.last_heartbeat = _now()
        await self.broadcast_status(state)

    async def disconnect_student(self, student_id: str) -> None:
        self.student_sockets.pop(student_id, None)
        state = self.states.get(student_id)
        if state:
            state.status = "offline"
            await self.broadcast_status(state)

    def record_heartbeat(self, student_id: str) -> None:
        state = self.states.get(student_id)
        if state:
            state.last_heartbeat = _now()

    async def record_event(self, event: dict[str, Any]) -> None:
        student_id = event["student_id"]
        state = self.states.get(student_id)
        if state is None:
            state = StudentState(
                student_id=student_id,
                display_name=student_id,
                session_id=event.get("session_id", "unknown"),
            )
            self.states[student_id] = state

        severity = Severity(event["severity"])
        state.last_event_type = event["event_type"]
        timestamp = event["timestamp"]
        state.last_event_at = dt.datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else timestamp
        state.current_state = event["event_type"]
        if severity in (Severity.YELLOW, Severity.RED):
            state.alert_count += 1
        if _SEVERITY_RANK[severity] > _SEVERITY_RANK[state.overall_severity]:
            state.overall_severity = severity

        self.recent_events.append(event)
        if len(self.recent_events) > self._max_recent_events:
            self.recent_events.pop(0)

        await self.broadcast_event(event)
        await self.broadcast_status(state)

    def reset_student_severity(self, student_id: str) -> None:
        """Used when a proctor acknowledges/clears a student's alerts (dashboard action)."""
        state = self.states.get(student_id)
        if state:
            state.overall_severity = Severity.GREEN
            state.alert_count = 0

    # ---------------- admin lifecycle ----------------

    async def connect_admin(self, websocket: WebSocket) -> None:
        self.admin_sockets.add(websocket)

    def disconnect_admin(self, websocket: WebSocket) -> None:
        self.admin_sockets.discard(websocket)

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "SNAPSHOT",
            "students": [s.to_dict() for s in self.states.values()],
            "recent_events": self.recent_events[-50:],
        }

    # ---------------- broadcasting ----------------

    async def broadcast_status(self, state: StudentState) -> None:
        message = {"type": "STUDENT_STATUS", **state.to_dict()}
        await self._broadcast_admins(message)

    async def broadcast_event(self, event: dict[str, Any]) -> None:
        message = {"type": "PROCTOR_EVENT", "payload": event}
        await self._broadcast_admins(message)

    async def _broadcast_admins(self, message: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.admin_sockets):
            try:
                await ws.send_json(message, mode="text")
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.admin_sockets.discard(ws)

    async def send_to_student(self, student_id: str, message: dict[str, Any]) -> bool:
        ws = self.student_sockets.get(student_id)
        if ws is None:
            return False
        try:
            await ws.send_json(message, mode="text")
            return True
        except Exception:
            return False

    def offline_candidates(self, timeout_seconds: int) -> list[str]:
        cutoff = _now() - dt.timedelta(seconds=timeout_seconds)
        result = []
        for student_id, state in self.states.items():
            if state.status == "online" and state.last_heartbeat and state.last_heartbeat < cutoff:
                result.append(student_id)
        return result


manager = ConnectionManager()
