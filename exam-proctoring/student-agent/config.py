"""Student Agent configuration.

Loaded from environment variables / a `.env` file, with CLI overrides applied
in main.py. Session credentials (session_id, student_id, token) are normally
produced by the proctor's enrollment step (server: POST /api/sessions/{id}/students)
and handed to the student out-of-band (e.g. pasted into a login screen or an
`enrollment.json` file) — the agent never has proctor/admin privileges.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    val = os.environ.get(name)
    if val is None:
        return default
    return tuple(item.strip() for item in val.split(",") if item.strip())


@dataclass
class AgentConfig:
    # Identity / connection (filled in by main.py from CLI args or enrollment file)
    server_ws_url: str = os.environ.get("SERVER_WS_URL", "ws://127.0.0.1:8000")
    server_http_url: str = os.environ.get("SERVER_HTTP_URL", "http://127.0.0.1:8000")
    session_id: str = os.environ.get("EXAM_SESSION_ID", "")
    student_id: str = os.environ.get("STUDENT_ID", "")
    student_name: str = os.environ.get("STUDENT_NAME", "")
    token: str = os.environ.get("STUDENT_TOKEN", "")

    # Networking
    heartbeat_interval_seconds: float = _env_float("HEARTBEAT_INTERVAL_SECONDS", 5.0)
    reconnect_initial_backoff_seconds: float = _env_float("RECONNECT_INITIAL_BACKOFF", 1.0)
    reconnect_max_backoff_seconds: float = _env_float("RECONNECT_MAX_BACKOFF", 30.0)
    connection_timeout_seconds: float = _env_float("CONNECTION_TIMEOUT_SECONDS", 10.0)

    # Local durability
    local_queue_path: str = os.environ.get("LOCAL_QUEUE_PATH", "agent_event_queue.jsonl")
    log_path: str = os.environ.get("AGENT_LOG_PATH", "agent.log")

    # Window monitoring
    window_monitor_enabled: bool = _env_bool("WINDOW_MONITOR_ENABLED", True)
    window_poll_interval_seconds: float = _env_float("WINDOW_POLL_INTERVAL_SECONDS", 1.0)

    # Display monitoring
    display_monitor_enabled: bool = _env_bool("DISPLAY_MONITOR_ENABLED", True)
    display_poll_interval_seconds: float = _env_float("DISPLAY_POLL_INTERVAL_SECONDS", 3.0)

    # Camera / face / gaze monitoring
    camera_monitor_enabled: bool = _env_bool("CAMERA_MONITOR_ENABLED", True)
    camera_index: int = _env_int("CAMERA_INDEX", 0)
    camera_frame_interval_seconds: float = _env_float("CAMERA_FRAME_INTERVAL_SECONDS", 0.5)
    no_face_threshold_seconds: float = _env_float("NO_FACE_THRESHOLD_SECONDS", 5.0)
    multiple_face_threshold_seconds: float = _env_float("MULTIPLE_FACE_THRESHOLD_SECONDS", 2.0)
    face_away_threshold_seconds: float = _env_float("FACE_AWAY_THRESHOLD_SECONDS", 5.0)
    prolonged_absence_threshold_seconds: float = _env_float("PROLONGED_ABSENCE_THRESHOLD_SECONDS", 30.0)
    look_away_threshold_seconds: float = _env_float("LOOK_AWAY_THRESHOLD_SECONDS", 7.0)

    # Process monitoring (bypass / mirroring detection): flags known remote-access
    # and virtual-camera tools running on this machine. Detection only - nothing
    # is killed or blocked. Video-calling apps (Zoom/Teams/Discord/Meet) are
    # deliberately excluded by default since a study group may legitimately want
    # a call running; add them here if your group wants that flagged too.
    process_monitor_enabled: bool = _env_bool("PROCESS_MONITOR_ENABLED", True)
    process_scan_interval_seconds: float = _env_float("PROCESS_SCAN_INTERVAL_SECONDS", 5.0)
    remote_access_process_keywords: tuple[str, ...] = _env_tuple(
        "REMOTE_ACCESS_PROCESS_KEYWORDS",
        (
            "teamviewer", "anydesk", "splashtop", "logmein", "gotomypc",
            "vncserver", "tigervnc", "realvnc", "x11vnc", "vino-server",
            "chrome_remote_desktop_host", "remoting_host",
        ),
    )
    virtual_camera_process_keywords: tuple[str, ...] = _env_tuple(
        "VIRTUAL_CAMERA_PROCESS_KEYWORDS",
        (
            "obs64", "obs32", "obs", "snapcamera", "manycam", "camtwist",
            "xsplit", "iriunwebcam", "iriun", "droidcam", "epoccam",
        ),
    )

    # If True, a second/mirrored display is treated as RED the moment it's
    # seen instead of the default YELLOW for a plain extended display - some
    # groups want any extra screen flagged hard, others just want awareness.
    treat_multi_display_as_critical: bool = _env_bool("TREAT_MULTI_DISPLAY_AS_CRITICAL", False)

    # Event engine cooldowns (seconds) - minimum gap between two emitted events of the same type
    default_cooldown_seconds: float = _env_float("DEFAULT_COOLDOWN_SECONDS", 10.0)
    cooldown_seconds: dict = field(
        default_factory=lambda: {
            "WINDOW_FOCUS_CHANGED": 3.0,
            "APPLICATION_CHANGED": 3.0,
            "BROWSER_FOCUS_LOST": 3.0,
            "NO_FACE_DETECTED": 8.0,
            "MULTIPLE_FACES_DETECTED": 8.0,
            "FACE_AWAY": 8.0,
            "PROLONGED_LOOK_AWAY": 10.0,
            "DISPLAY_MIRRORING_DETECTED": 15.0,
            "EXTERNAL_DISPLAY_DETECTED": 15.0,
            "DISPLAY_CONFIGURATION_CHANGED": 5.0,
            "CAMERA_UNAVAILABLE": 20.0,
            "REMOTE_ACCESS_TOOL_DETECTED": 20.0,
            "VIRTUAL_CAMERA_TOOL_DETECTED": 20.0,
            "PROCESS_MONITOR_UNAVAILABLE": 30.0,
        }
    )

    # Evidence snapshots
    evidence_capture_enabled: bool = _env_bool("EVIDENCE_CAPTURE_ENABLED", True)
    evidence_capture_severities: tuple = ("red",)
    evidence_max_dimension: int = _env_int("EVIDENCE_MAX_DIMENSION", 640)
    evidence_jpeg_quality: int = _env_int("EVIDENCE_JPEG_QUALITY", 60)

    def is_complete(self) -> bool:
        return bool(self.session_id and self.student_id and self.token)
