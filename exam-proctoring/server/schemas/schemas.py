"""Pydantic schemas for REST + WebSocket payloads, and the canonical enums.

Severity uses the three-level GREEN/YELLOW/RED system described in the spec
(section 8). Earlier illustrative examples in some specs use ad-hoc labels
like "medium"/"high" — this implementation standardizes on green/yellow/red
everywhere (wire protocol, DB, dashboard) for consistency.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class EventType(str, Enum):
    # Window / application monitoring
    WINDOW_FOCUS_CHANGED = "WINDOW_FOCUS_CHANGED"
    BROWSER_FOCUS_LOST = "BROWSER_FOCUS_LOST"
    APPLICATION_CHANGED = "APPLICATION_CHANGED"
    WINDOW_MONITOR_UNAVAILABLE = "WINDOW_MONITOR_UNAVAILABLE"

    # Display / screen mirroring
    DISPLAY_CONFIGURATION_CHANGED = "DISPLAY_CONFIGURATION_CHANGED"
    DISPLAY_MIRRORING_DETECTED = "DISPLAY_MIRRORING_DETECTED"
    EXTERNAL_DISPLAY_DETECTED = "EXTERNAL_DISPLAY_DETECTED"
    DISPLAY_MONITOR_UNAVAILABLE = "DISPLAY_MONITOR_UNAVAILABLE"

    # Face / webcam monitoring
    FACE_PRESENT = "FACE_PRESENT"
    ONE_FACE_DETECTED = "ONE_FACE_DETECTED"
    NO_FACE_DETECTED = "NO_FACE_DETECTED"
    MULTIPLE_FACES_DETECTED = "MULTIPLE_FACES_DETECTED"
    FACE_AWAY = "FACE_AWAY"
    POSSIBLE_PROLONGED_ABSENCE = "POSSIBLE_PROLONGED_ABSENCE"
    CAMERA_UNAVAILABLE = "CAMERA_UNAVAILABLE"

    # Gaze / head pose
    PROLONGED_LOOK_AWAY = "PROLONGED_LOOK_AWAY"

    # Bypass / mirroring detection (running processes on the student's own machine)
    REMOTE_ACCESS_TOOL_DETECTED = "REMOTE_ACCESS_TOOL_DETECTED"
    VIRTUAL_CAMERA_TOOL_DETECTED = "VIRTUAL_CAMERA_TOOL_DETECTED"
    PROCESS_MONITOR_UNAVAILABLE = "PROCESS_MONITOR_UNAVAILABLE"

    # Lifecycle / system
    MONITORING_PERMISSION_DENIED = "MONITORING_PERMISSION_DENIED"
    STUDENT_CONNECTED = "STUDENT_CONNECTED"
    STUDENT_DISCONNECTED = "STUDENT_DISCONNECTED"
    EXAM_STARTED = "EXAM_STARTED"
    EXAM_ENDED = "EXAM_ENDED"


# Default severity per event type. The student-agent's EventEngine may escalate
# (e.g. yellow -> red) based on persistence/repetition; the server trusts the
# severity it receives but clamps it to a known value.
DEFAULT_SEVERITY: dict[EventType, Severity] = {
    EventType.WINDOW_FOCUS_CHANGED: Severity.YELLOW,
    EventType.BROWSER_FOCUS_LOST: Severity.YELLOW,
    EventType.APPLICATION_CHANGED: Severity.YELLOW,
    EventType.WINDOW_MONITOR_UNAVAILABLE: Severity.YELLOW,
    EventType.DISPLAY_MONITOR_UNAVAILABLE: Severity.YELLOW,
    EventType.DISPLAY_CONFIGURATION_CHANGED: Severity.YELLOW,
    EventType.DISPLAY_MIRRORING_DETECTED: Severity.RED,
    EventType.EXTERNAL_DISPLAY_DETECTED: Severity.YELLOW,
    EventType.FACE_PRESENT: Severity.GREEN,
    EventType.ONE_FACE_DETECTED: Severity.GREEN,
    EventType.NO_FACE_DETECTED: Severity.YELLOW,
    EventType.MULTIPLE_FACES_DETECTED: Severity.RED,
    EventType.FACE_AWAY: Severity.YELLOW,
    EventType.POSSIBLE_PROLONGED_ABSENCE: Severity.RED,
    EventType.CAMERA_UNAVAILABLE: Severity.YELLOW,
    EventType.PROLONGED_LOOK_AWAY: Severity.YELLOW,
    EventType.REMOTE_ACCESS_TOOL_DETECTED: Severity.RED,
    EventType.VIRTUAL_CAMERA_TOOL_DETECTED: Severity.RED,
    EventType.PROCESS_MONITOR_UNAVAILABLE: Severity.YELLOW,
    EventType.MONITORING_PERMISSION_DENIED: Severity.YELLOW,
    EventType.STUDENT_CONNECTED: Severity.GREEN,
    EventType.STUDENT_DISCONNECTED: Severity.YELLOW,
    EventType.EXAM_STARTED: Severity.GREEN,
    EventType.EXAM_ENDED: Severity.GREEN,
}


class EventPayload(BaseModel):
    event_id: str
    student_id: str
    session_id: str
    timestamp: dt.datetime
    event_type: EventType
    severity: Severity
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_id: Optional[str] = None


class ClientMessageType(str, Enum):
    AUTH = "AUTH"
    EVENT = "EVENT"
    HEARTBEAT = "HEARTBEAT"
    CONSENT = "CONSENT"
    END_EXAM = "END_EXAM"


class ServerMessageType(str, Enum):
    AUTH_OK = "AUTH_OK"
    AUTH_FAILED = "AUTH_FAILED"
    EVENT_ACK = "EVENT_ACK"
    HEARTBEAT_ACK = "HEARTBEAT_ACK"
    STUDENT_STATUS = "STUDENT_STATUS"
    PROCTOR_EVENT = "PROCTOR_EVENT"
    SNAPSHOT = "SNAPSHOT"
    ERROR = "ERROR"


class AuthMessage(BaseModel):
    type: ClientMessageType = ClientMessageType.AUTH
    session_id: str
    student_id: str
    token: str


class HeartbeatMessage(BaseModel):
    type: ClientMessageType = ClientMessageType.HEARTBEAT
    student_id: str
    timestamp: dt.datetime


class EventMessage(BaseModel):
    type: ClientMessageType = ClientMessageType.EVENT
    payload: EventPayload


# ---- REST schemas ----


# Session and student ids become path components under the evidence storage
# directory, so they must not be able to escape it. Requiring the first
# character to be alphanumeric (rather than just allowlisting the character
# set) is what rules out "." and ".." - both of which are made up entirely of
# otherwise-permitted characters and would traverse upward. Also keeps ids
# safe to embed in URLs and filenames.
SAFE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class CreateSessionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    session_id: Optional[str] = Field(default=None, pattern=SAFE_ID_PATTERN)


class CreateSessionResponse(BaseModel):
    session_id: str
    name: str
    created_at: dt.datetime


class EnrollRequest(BaseModel):
    session_id: str = Field(pattern=SAFE_ID_PATTERN)
    student_id: str = Field(pattern=SAFE_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)


class EnrollResponse(BaseModel):
    session_id: str
    student_id: str
    token: str
    expires_at: dt.datetime


class StudentStatusOut(BaseModel):
    student_id: str
    display_name: str
    session_id: str
    status: str  # online | offline
    current_state: str
    overall_severity: Severity
    alert_count: int
    last_event_type: Optional[str] = None
    last_event_at: Optional[dt.datetime] = None
    last_heartbeat: Optional[dt.datetime] = None
