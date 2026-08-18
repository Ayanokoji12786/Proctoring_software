"""Student Agent entrypoint.

Wires together: the WebSocket client (networking), the EventEngine, the
window/display/camera monitors, and the Tkinter UI. Tkinter must own the
main thread, so all networking + monitoring runs on a background asyncio
event loop in a second thread; the camera monitor gets its own OS thread
(OpenCV capture is blocking) and hands signals back to the asyncio loop via
`call_soon_threadsafe`.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import platform
import sys
import threading
import tkinter as tk
from typing import Optional

from config import AgentConfig
from events.engine import EventEngine
from events.schemas import EventPayload, EventType, Severity, Signal
from monitors.camera_monitor import CameraMonitor, CameraMonitorConfig
from monitors.display_monitor import DisplayActivityTracker, get_display_monitor
from monitors.process_monitor import ProcessActivityTracker, ProcessMonitor
from monitors.window_monitor import WindowActivityTracker, get_window_monitor
from networking.evidence_uploader import upload_snapshot
from networking.websocket_client import WebSocketClient
from ui.app import AgentWindow, show_consent_dialog

logger = logging.getLogger("agent.main")


def setup_logging(config: AgentConfig) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(config.log_path, maxBytes=2_000_000, backupCount=3)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    # Never log frame/image bytes or full event metadata blobs that could carry
    # incidental personal data beyond what's needed for diagnostics.
    logging.getLogger("agent.monitors.camera").setLevel(logging.INFO)


def parse_args() -> AgentConfig:
    parser = argparse.ArgumentParser(description="Exam Proctoring Student Agent")
    parser.add_argument("--server", dest="server_ws_url", help="WebSocket server base URL, e.g. ws://localhost:8000")
    parser.add_argument("--http-server", dest="server_http_url", help="HTTP server base URL, e.g. http://localhost:8000")
    parser.add_argument("--session-id", dest="session_id")
    parser.add_argument("--student-id", dest="student_id")
    parser.add_argument("--student-name", dest="student_name")
    parser.add_argument("--token", dest="token")
    parser.add_argument("--no-camera", action="store_true", help="Disable webcam/face monitoring")
    parser.add_argument("--no-window-monitor", action="store_true")
    parser.add_argument("--no-display-monitor", action="store_true")
    parser.add_argument("--no-process-monitor", action="store_true", help="Disable remote-access/virtual-camera tool detection")
    args = parser.parse_args()

    config = AgentConfig()
    if args.server_ws_url:
        config.server_ws_url = args.server_ws_url
    if args.server_http_url:
        config.server_http_url = args.server_http_url
    if args.session_id:
        config.session_id = args.session_id
    if args.student_id:
        config.student_id = args.student_id
    if args.student_name:
        config.student_name = args.student_name
    if args.token:
        config.token = args.token
    if args.no_camera:
        config.camera_monitor_enabled = False
    if args.no_window_monitor:
        config.window_monitor_enabled = False
    if args.no_display_monitor:
        config.display_monitor_enabled = False
    if args.no_process_monitor:
        config.process_monitor_enabled = False
    return config


def preauthorize_camera_if_needed(camera_index: int) -> None:
    """Resolve macOS camera authorization before CameraMonitor's background
    thread ever touches the camera - see monitors/camera_permissions.py for
    why this can't just happen lazily inside CameraMonitor itself (the short
    version: AVFoundation can only show/await the permission dialog from the
    main thread, and OpenCV's own request-and-check doesn't wait for the
    user's answer at all). No-op on non-macOS platforms.
    """
    from monitors.camera_permissions import ensure_camera_authorized

    granted = ensure_camera_authorized()
    if not granted and platform.system() == "Darwin":
        logger.warning("camera not authorized; camera/face monitoring will report CAMERA_UNAVAILABLE")


class AgentApp:
    def __init__(self, config: AgentConfig, ui: AgentWindow, loop: asyncio.AbstractEventLoop) -> None:
        self.config = config
        self.ui = ui
        self.loop = loop
        self._stopped = False
        self._tasks: list[asyncio.Task] = []

        self.ws_client = WebSocketClient(config, on_status_change=self._on_ws_status)
        self.engine = EventEngine(config.session_id, config.student_id, config, on_emit=self._on_event_emitted)

        self.window_monitor = get_window_monitor()
        self.window_tracker = WindowActivityTracker()
        self.display_monitor = get_display_monitor()
        self.display_tracker = DisplayActivityTracker()
        self.process_monitor = ProcessMonitor(
            config.remote_access_process_keywords, config.virtual_camera_process_keywords
        )
        self.process_tracker = ProcessActivityTracker()

        self.camera_monitor: Optional[CameraMonitor] = None
        if config.camera_monitor_enabled:
            cam_config = CameraMonitorConfig(
                camera_index=config.camera_index,
                frame_interval_seconds=config.camera_frame_interval_seconds,
                no_face_threshold_seconds=config.no_face_threshold_seconds,
                multiple_face_threshold_seconds=config.multiple_face_threshold_seconds,
                face_away_threshold_seconds=config.face_away_threshold_seconds,
                prolonged_absence_threshold_seconds=config.prolonged_absence_threshold_seconds,
                look_away_threshold_seconds=config.look_away_threshold_seconds,
            )
            self.camera_monitor = CameraMonitor(
                cam_config,
                on_signal=self._on_camera_signal_threadsafe,
                on_state_change=self._on_camera_state_threadsafe,
            )

    # ---- WebSocketClient callbacks (run on the asyncio loop thread) ----
    def _on_ws_status(self, status: str) -> None:
        self.ui.push_update(connection=status)
        logger.info("connection status changed: %s", status)

    # ---- EventEngine callback (asyncio loop thread) ----
    def _on_event_emitted(self, event: EventPayload) -> None:
        self.ws_client.enqueue_event(event)
        self.ui.push_update(event_count=self.engine.total_events_emitted)

        if (
            self.camera_monitor is not None
            and self.config.evidence_capture_enabled
            and event.severity.value in self.config.evidence_capture_severities
        ):
            asyncio.ensure_future(self._capture_and_upload_evidence(event), loop=self.loop)

    async def _capture_and_upload_evidence(self, event: EventPayload) -> None:
        assert self.camera_monitor is not None
        jpeg = await self.loop.run_in_executor(
            None,
            self.camera_monitor.get_last_frame_jpeg,
            self.config.evidence_max_dimension,
            self.config.evidence_jpeg_quality,
        )
        if jpeg is None:
            return
        await upload_snapshot(self.config, event.event_id, jpeg)

    # ---- Camera thread callbacks (NOT the asyncio loop thread) ----
    def _on_camera_signal_threadsafe(self, event_type: str, metadata: dict, severity_hint: Optional[Severity]) -> None:
        self.loop.call_soon_threadsafe(self._handle_signal, "camera", event_type, metadata, severity_hint)

    def _on_camera_state_threadsafe(self, state: str) -> None:
        camera_status = "unavailable" if state == "UNAVAILABLE" else "active"
        self.ui.push_update(current_state=state, camera=camera_status)

    # ---- shared signal handling (must run on the asyncio loop thread) ----
    def _handle_signal(self, source: str, event_type_name: str, metadata: dict, severity_hint: Optional[Severity] = None) -> None:
        try:
            event_type = EventType(event_type_name)
        except ValueError:
            logger.warning("unknown event type from %s monitor: %s", source, event_type_name)
            return
        signal = Signal(source=source, event_type=event_type, metadata=metadata, severity_hint=severity_hint)
        self.engine.process_signal(signal)
        self.ui.push_update(current_state=event_type_name)

    # ---- window / display polling (async tasks on the loop) ----
    async def _window_polling_loop(self) -> None:
        if not self.config.window_monitor_enabled:
            return
        self.window_monitor.start()
        while not self._stopped:
            info = await self.loop.run_in_executor(None, self.window_monitor.get_active_window)
            result = self.window_tracker.observe(info)
            if result:
                self._handle_signal("window", result[0], result[1])
            self.ui.push_update(monitoring="active" if info.available else "unavailable")
            await asyncio.sleep(self.config.window_poll_interval_seconds)

    async def _display_polling_loop(self) -> None:
        if not self.config.display_monitor_enabled:
            return
        self.display_monitor.start()
        while not self._stopped:
            state = await self.loop.run_in_executor(None, self.display_monitor.get_display_state)
            for event_type_name, metadata in self.display_tracker.observe(state):
                severity_hint = None
                if event_type_name == "EXTERNAL_DISPLAY_DETECTED" and self.config.treat_multi_display_as_critical:
                    severity_hint = Severity.RED
                self._handle_signal("display", event_type_name, metadata, severity_hint)
            await asyncio.sleep(self.config.display_poll_interval_seconds)

    async def _process_polling_loop(self) -> None:
        if not self.config.process_monitor_enabled:
            return
        while not self._stopped:
            result = await self.loop.run_in_executor(None, self.process_monitor.scan)
            for event_type_name, metadata in self.process_tracker.observe(result):
                self._handle_signal("process", event_type_name, metadata)
            await asyncio.sleep(self.config.process_scan_interval_seconds)

    async def start(self) -> None:
        logger.info("exam started session_id=%s student_id=%s", self.config.session_id, self.config.student_id)
        self.engine.process_signal(Signal(source="system", event_type=EventType.EXAM_STARTED, metadata={}))

        self._tasks = [
            asyncio.ensure_future(self.ws_client.run()),
            asyncio.ensure_future(self._window_polling_loop()),
            asyncio.ensure_future(self._display_polling_loop()),
            asyncio.ensure_future(self._process_polling_loop()),
        ]
        if self.camera_monitor is not None:
            self.camera_monitor.start()
            self.ui.push_update(camera="connecting")
        else:
            self.ui.push_update(camera="disabled")

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        logger.info("exam ending session_id=%s student_id=%s", self.config.session_id, self.config.student_id)
        self.engine.process_signal(Signal(source="system", event_type=EventType.EXAM_ENDED, metadata={}))
        await asyncio.sleep(0.3)  # brief grace period to flush the EXAM_ENDED event if connected

        if self.camera_monitor is not None:
            self.camera_monitor.stop()
        self.window_monitor.stop()
        self.display_monitor.stop()
        await self.ws_client.stop(graceful=True)

        for task in self._tasks:
            task.cancel()


def run_asyncio_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main() -> None:
    config = parse_args()
    setup_logging(config)
    logger.info("agent starting up session_id=%s student_id=%s", config.session_id, config.student_id)

    if not config.is_complete():
        print("Missing required configuration. Provide --session-id, --student-id and --token")
        print("(or set EXAM_SESSION_ID / STUDENT_ID / STUDENT_TOKEN environment variables).")
        print("Tokens are issued by the proctor via POST /api/sessions/{session_id}/students on the server.")
        sys.exit(1)

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=run_asyncio_loop, args=(loop,), daemon=True, name="agent-asyncio-loop")
    loop_thread.start()

    ui = AgentWindow(config.session_id, config.student_id, on_end_exam=lambda: None)
    ui.withdraw()
    consented = show_consent_dialog(ui)
    if not consented:
        logger.info("student declined consent; exiting without starting monitoring")
        ui.destroy()
        loop.call_soon_threadsafe(loop.stop)
        sys.exit(0)
    ui.deiconify()
    ui.bring_to_front()

    if config.camera_monitor_enabled:
        preauthorize_camera_if_needed(config.camera_index)

    app = AgentApp(config, ui, loop)

    def handle_end_exam() -> None:
        logger.info("student clicked End Exam")
        future = asyncio.run_coroutine_threadsafe(app.stop(), loop)
        future.add_done_callback(lambda f: ui.after(0, ui.destroy))

    ui._on_end_exam = handle_end_exam  # bind now that app exists (avoids a circular constructor dependency)

    asyncio.run_coroutine_threadsafe(app.start(), loop)

    try:
        ui.mainloop()
    finally:
        if not app._stopped:
            try:
                asyncio.run_coroutine_threadsafe(app.stop(), loop).result(timeout=5)
            except Exception:
                logger.exception("error while stopping agent during shutdown")
        loop.call_soon_threadsafe(loop.stop)
        logger.info("agent shut down")


if __name__ == "__main__":
    main()
