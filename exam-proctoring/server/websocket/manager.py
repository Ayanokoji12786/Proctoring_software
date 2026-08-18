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
    """Tracks live connections and fans events out to admin dashboards.

    Everything is keyed by (session_id, student_id) rather than student_id
    alone, and every admin socket is bound to the one exam session it asked to
    watch. Keying by student_id alone both collided when the same student_id
    appeared in two exams and - more seriously - let a proctor watching exam A
    receive the students and events of exam B.
    """

    def __init__(self) -> None:
        self.student_sockets: dict[tuple[str, str], WebSocket] = {}
        # admin socket -> the session_id that dashboard is scoped to
        self.admin_sockets: dict[WebSocket, str] = {}
        self.states: dict[tuple[str, str], StudentState] = {}
        self.recent_events: dict[str, list[dict[str, Any]]] = {}
        self._max_recent_events = 200

    @staticmethod
    def _key(session_id: str, student_id: str) -> tuple[str, str]:
        return (session_id, student_id)

    # ---------------- student lifecycle ----------------

    async def connect_student(self, student_id: str, display_name: str, session_id: str, websocket: WebSocket) -> None:
        key = self._key(session_id, student_id)
        self.student_sockets[key] = websocket
        state = self.states.get(key)
        if state is None:
            state = StudentState(student_id=student_id, display_name=display_name, session_id=session_id)
            self.states[key] = state
        state.status = "online"
        state.session_id = session_id
        state.display_name = display_name
        state.connected_at = _now()
        state.last_heartbeat = _now()
        await self.broadcast_status(state)

    async def disconnect_student(self, student_id: str, session_id: str, websocket: WebSocket | None = None) -> None:
        """Deregisters a student connection.

        `websocket` identifies *which* connection is going away. On a fast
        reconnect the replacement socket registers before the dropped one's
        cleanup runs; without this check that late cleanup would evict the new
        socket and flip a genuinely-connected student to "offline" on the
        dashboard. A stale socket is therefore ignored.
        """
        key = self._key(session_id, student_id)
        current = self.student_sockets.get(key)
        if websocket is not None and current is not None and current is not websocket:
            logger.debug("ignoring stale disconnect for %s (superseded by a newer connection)", student_id)
            return

        self.student_sockets.pop(key, None)
        state = self.states.get(key)
        if state:
            state.status = "offline"
            await self.broadcast_status(state)

    def record_heartbeat(self, student_id: str, session_id: str) -> None:
        state = self.states.get(self._key(session_id, student_id))
        if state:
            state.last_heartbeat = _now()

    async def record_event(self, event: dict[str, Any]) -> None:
        student_id = event["student_id"]
        session_id = event.get("session_id", "unknown")
        key = self._key(session_id, student_id)
        state = self.states.get(key)
        if state is None:
            state = StudentState(student_id=student_id, display_name=student_id, session_id=session_id)
            self.states[key] = state

        severity = Severity(event["severity"])
        state.last_event_type = event["event_type"]
        timestamp = event["timestamp"]
        state.last_event_at = dt.datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else timestamp
        state.current_state = event["event_type"]
        if severity in (Severity.YELLOW, Severity.RED):
            state.alert_count += 1
        if _SEVERITY_RANK[severity] > _SEVERITY_RANK[state.overall_severity]:
            state.overall_severity = severity

        feed = self.recent_events.setdefault(session_id, [])
        feed.append(event)
        if len(feed) > self._max_recent_events:
            feed.pop(0)

        await self.broadcast_event(event, session_id)
        await self.broadcast_status(state)

    def reset_student_severity(self, student_id: str, session_id: str) -> None:
        """Used when a proctor acknowledges/clears a student's alerts (dashboard action)."""
        state = self.states.get(self._key(session_id, student_id))
        if state:
            state.overall_severity = Severity.GREEN
            state.alert_count = 0

    def get_state(self, student_id: str, session_id: str) -> Optional[StudentState]:
        return self.states.get(self._key(session_id, student_id))

    async def close_session(self, session_id: str) -> int:
        """Disconnects every agent in a session and drops its in-memory state.

        Called when a proctor ends an exam: monitoring must not continue after
        the exam is over, and the per-session state/feed would otherwise sit in
        memory for the lifetime of the process.
        """
        sockets = [(key, ws) for key, ws in self.student_sockets.items() if key[0] == session_id]
        for key, ws in sockets:
            try:
                await ws.send_json({"type": "SESSION_ENDED", "session_id": session_id}, mode="text")
                await ws.close(code=1000)
            except Exception:
                pass  # already gone; the handler's own cleanup will run
            self.student_sockets.pop(key, None)

        for key in [k for k in self.states if k[0] == session_id]:
            self.states.pop(key, None)
        self.recent_events.pop(session_id, None)

        logger.info("closed session %s, disconnected %d agent(s)", session_id, len(sockets))
        return len(sockets)

    # ---------------- admin lifecycle ----------------

    async def connect_admin(self, websocket: WebSocket, session_id: str) -> None:
        """Binds an admin dashboard to exactly one exam session's stream."""
        self.admin_sockets[websocket] = session_id

    def disconnect_admin(self, websocket: WebSocket) -> None:
        self.admin_sockets.pop(websocket, None)

    def snapshot(self, session_id: str) -> dict[str, Any]:
        return {
            "type": "SNAPSHOT",
            "session_id": session_id,
            "students": [s.to_dict() for k, s in self.states.items() if k[0] == session_id],
            "recent_events": self.recent_events.get(session_id, [])[-50:],
        }

    # ---------------- broadcasting ----------------

    async def broadcast_status(self, state: StudentState) -> None:
        message = {"type": "STUDENT_STATUS", **state.to_dict()}
        await self._broadcast_admins(message, state.session_id)

    async def broadcast_event(self, event: dict[str, Any], session_id: str) -> None:
        message = {"type": "PROCTOR_EVENT", "payload": event}
        await self._broadcast_admins(message, session_id)

    async def _broadcast_admins(self, message: dict[str, Any], session_id: str) -> None:
        """Delivers only to dashboards watching `session_id` - never to a proctor
        monitoring a different exam."""
        dead: list[WebSocket] = []
        for ws, watched_session in list(self.admin_sockets.items()):
            if watched_session != session_id:
                continue
            try:
                await ws.send_json(message, mode="text")
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.admin_sockets.pop(ws, None)

    def offline_candidates(self, timeout_seconds: int) -> list[tuple[str, str]]:
        """Returns (session_id, student_id) pairs whose heartbeat has lapsed."""
        cutoff = _now() - dt.timedelta(seconds=timeout_seconds)
        return [
            key
            for key, state in self.states.items()
            if state.status == "online" and state.last_heartbeat and state.last_heartbeat < cutoff
        ]


manager = ConnectionManager()
