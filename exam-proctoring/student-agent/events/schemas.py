"""Event schemas and enums for the Student Agent.

Mirrors the server's wire contract (server/schemas/schemas.py). The two
components are deployed independently (agent runs on a student's machine,
server runs centrally) so this is intentionally a self-contained copy rather
than a shared import — the JSON wire format is the contract between them.
Keep the two in sync; tests/test_event_schema_parity.py checks this.
"""
from __future__ import annotations

import datetime as dt
import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class EventType(str, Enum):
    WINDOW_FOCUS_CHANGED = "WINDOW_FOCUS_CHANGED"
    BROWSER_FOCUS_LOST = "BROWSER_FOCUS_LOST"
    APPLICATION_CHANGED = "APPLICATION_CHANGED"
    WINDOW_MONITOR_UNAVAILABLE = "WINDOW_MONITOR_UNAVAILABLE"

    DISPLAY_CONFIGURATION_CHANGED = "DISPLAY_CONFIGURATION_CHANGED"
    DISPLAY_MIRRORING_DETECTED = "DISPLAY_MIRRORING_DETECTED"
    EXTERNAL_DISPLAY_DETECTED = "EXTERNAL_DISPLAY_DETECTED"
    DISPLAY_MONITOR_UNAVAILABLE = "DISPLAY_MONITOR_UNAVAILABLE"

    FACE_PRESENT = "FACE_PRESENT"
    ONE_FACE_DETECTED = "ONE_FACE_DETECTED"
    NO_FACE_DETECTED = "NO_FACE_DETECTED"
    MULTIPLE_FACES_DETECTED = "MULTIPLE_FACES_DETECTED"
    FACE_AWAY = "FACE_AWAY"
    POSSIBLE_PROLONGED_ABSENCE = "POSSIBLE_PROLONGED_ABSENCE"
    CAMERA_UNAVAILABLE = "CAMERA_UNAVAILABLE"

    PROLONGED_LOOK_AWAY = "PROLONGED_LOOK_AWAY"

    REMOTE_ACCESS_TOOL_DETECTED = "REMOTE_ACCESS_TOOL_DETECTED"
    VIRTUAL_CAMERA_TOOL_DETECTED = "VIRTUAL_CAMERA_TOOL_DETECTED"
    PROCESS_MONITOR_UNAVAILABLE = "PROCESS_MONITOR_UNAVAILABLE"

    MONITORING_PERMISSION_DENIED = "MONITORING_PERMISSION_DENIED"
    STUDENT_CONNECTED = "STUDENT_CONNECTED"
    STUDENT_DISCONNECTED = "STUDENT_DISCONNECTED"
    EXAM_STARTED = "EXAM_STARTED"
    EXAM_ENDED = "EXAM_ENDED"


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
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    student_id: str
    session_id: str
    timestamp: dt.datetime
    event_type: EventType
    severity: Severity
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_id: Optional[str] = None


class Signal(BaseModel):
    """Raw observation from a monitor, before the EventEngine decides whether to emit it."""

    source: str  # "window" | "display" | "camera" | "gaze"
    event_type: EventType
    metadata: dict[str, Any] = Field(default_factory=dict)
    severity_hint: Optional[Severity] = None
    detected_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
