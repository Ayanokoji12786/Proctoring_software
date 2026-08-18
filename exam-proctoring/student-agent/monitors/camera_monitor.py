"""Webcam-based face presence and head-pose (gaze direction) monitoring.

Runs OpenCV frame capture + MediaPipe inference on a dedicated background
thread (both are blocking calls) and reports state transitions back to the
asyncio event loop via a thread-safe callback. All processing happens
locally on the student's machine — raw frames are never sent to the server;
only derived events (and, optionally, a single still-image snapshot for
high-severity events) leave the machine.

LIMITATIONS (see README): face detection can produce false positives and
negatives depending on lighting/camera quality; head-pose estimation here is
a basic geometric estimate (solvePnP against a generic face model), not
calibrated eye-tracking. "Looking away" is a signal for review, not proof of
misconduct.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from events.schemas import Severity

logger = logging.getLogger("agent.monitors.camera")

# MediaPipe Face Mesh landmark indices used for the solvePnP head-pose estimate.
_NOSE_TIP = 1
_CHIN = 152
_LEFT_EYE_OUTER = 33
_RIGHT_EYE_OUTER = 263
_LEFT_MOUTH_CORNER = 61
_RIGHT_MOUTH_CORNER = 291

# Generic 3D face model (arbitrary units), standard points used in head-pose demos.
_MODEL_POINTS = [
    (0.0, 0.0, 0.0),  # nose tip
    (0.0, -330.0, -65.0),  # chin
    (-225.0, 170.0, -135.0),  # left eye outer corner
    (225.0, 170.0, -135.0),  # right eye outer corner
    (-150.0, -150.0, -125.0),  # left mouth corner
    (150.0, -150.0, -125.0),  # right mouth corner
]

YAW_THRESHOLD_DEG = 15.0
PITCH_THRESHOLD_DEG = 12.0

CONSECUTIVE_READ_FAILURES_BEFORE_ALERT = 30  # ~ a few seconds at typical FPS; "100 frames" spirit from spec, tuned down for responsiveness
CAMERA_RETRY_INTERVAL_SECONDS = 5.0


@dataclass
class CameraMonitorConfig:
    camera_index: int
    frame_interval_seconds: float
    no_face_threshold_seconds: float
    multiple_face_threshold_seconds: float
    face_away_threshold_seconds: float
    prolonged_absence_threshold_seconds: float
    look_away_threshold_seconds: float


SignalCallback = Callable[[str, dict, Optional[Severity]], None]
StateChangeCallback = Callable[[str], None]


def _estimate_head_pose(landmarks, frame_w: int, frame_h: int):
    """Returns (direction, yaw_deg, pitch_deg) or None if pose can't be estimated."""
    import cv2
    import numpy as np

    try:
        image_points = np.array(
            [
                (landmarks[_NOSE_TIP].x * frame_w, landmarks[_NOSE_TIP].y * frame_h),
                (landmarks[_CHIN].x * frame_w, landmarks[_CHIN].y * frame_h),
                (landmarks[_LEFT_EYE_OUTER].x * frame_w, landmarks[_LEFT_EYE_OUTER].y * frame_h),
                (landmarks[_RIGHT_EYE_OUTER].x * frame_w, landmarks[_RIGHT_EYE_OUTER].y * frame_h),
                (landmarks[_LEFT_MOUTH_CORNER].x * frame_w, landmarks[_LEFT_MOUTH_CORNER].y * frame_h),
                (landmarks[_RIGHT_MOUTH_CORNER].x * frame_w, landmarks[_RIGHT_MOUTH_CORNER].y * frame_h),
            ],
            dtype="double",
        )
    except IndexError:
        return None

    model_points = np.array(_MODEL_POINTS, dtype="double")
    focal_length = frame_w
    center = (frame_w / 2, frame_h / 2)
    camera_matrix = np.array(
        [[focal_length, 0, center[0]], [0, focal_length, center[1]], [0, 0, 1]], dtype="double"
    )
    dist_coeffs = np.zeros((4, 1))

    success, rotation_vector, _translation_vector = cv2.solvePnP(
        model_points, image_points, camera_matrix, dist_coeffs, flags=cv2.SOLVEPNP_ITERATIVE
    )
    if not success:
        return None

    rmat, _ = cv2.Rodrigues(rotation_vector)
    sy = math.sqrt(rmat[0, 0] ** 2 + rmat[1, 0] ** 2)
    singular = sy < 1e-6
    if not singular:
        pitch = math.atan2(rmat[2, 1], rmat[2, 2])
        yaw = math.atan2(-rmat[2, 0], sy)
    else:
        pitch = math.atan2(-rmat[1, 2], rmat[1, 1])
        yaw = math.atan2(-rmat[2, 0], sy)
    yaw_deg, pitch_deg = math.degrees(yaw), math.degrees(pitch)

    if abs(yaw_deg) < YAW_THRESHOLD_DEG and abs(pitch_deg) < PITCH_THRESHOLD_DEG:
        direction = "center"
    elif abs(yaw_deg) >= abs(pitch_deg):
        direction = "right" if yaw_deg > 0 else "left"
    else:
        direction = "down" if pitch_deg > 0 else "up"

    return direction, yaw_deg, pitch_deg


class CameraMonitor:
    def __init__(
        self,
        config: CameraMonitorConfig,
        on_signal: SignalCallback,
        on_state_change: Optional[StateChangeCallback] = None,
    ) -> None:
        self.config = config
        self._on_signal = on_signal
        self._on_state_change = on_state_change or (lambda s: None)

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._last_frame = None
        self._cap = None

        self._no_face_since: Optional[float] = None
        self._prolonged_absence_reported = False
        self._multi_face_since: Optional[float] = None
        self._face_away_since: Optional[float] = None
        self._look_away_since: Optional[float] = None
        self._look_away_direction: Optional[str] = None
        self._consecutive_read_failures = 0
        self._camera_unavailable_reported = False

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="camera-monitor")
        self._thread.start()
        logger.info("camera monitor thread started (index=%d)", self.config.camera_index)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._cap is not None:
            self._cap.release()
        logger.info("camera monitor stopped")

    def get_last_frame_jpeg(self, max_dimension: int = 640, quality: int = 60) -> Optional[bytes]:
        import cv2

        with self._frame_lock:
            frame = None if self._last_frame is None else self._last_frame.copy()
        if frame is None:
            return None
        h, w = frame.shape[:2]
        scale = min(1.0, max_dimension / max(h, w))
        if scale < 1.0:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if ok else None

    def _emit(self, event_type: str, metadata: dict, severity_hint: Optional[Severity] = None) -> None:
        self._on_signal(event_type, metadata, severity_hint)

    def _run_loop(self) -> None:
        try:
            import cv2
        except ImportError as exc:
            logger.error("OpenCV not installed: %s", exc)
            self._emit("CAMERA_UNAVAILABLE", {"reason": f"opencv-python not installed: {exc}"}, Severity.YELLOW)
            self._on_state_change("UNAVAILABLE")
            return

        try:
            import mediapipe as mp
        except ImportError as exc:
            logger.error("MediaPipe not installed: %s", exc)
            self._emit("CAMERA_UNAVAILABLE", {"reason": f"mediapipe not installed: {exc}"}, Severity.YELLOW)
            self._on_state_change("UNAVAILABLE")
            return

        self._cap = cv2.VideoCapture(self.config.camera_index)
        if not self._cap.isOpened():
            self._emit(
                "CAMERA_UNAVAILABLE",
                {"reason": "could not open webcam (not present, in use by another app, or OS permission denied)"},
                Severity.YELLOW,
            )
            self._on_state_change("UNAVAILABLE")
            logger.warning("camera failed to open at startup; will keep retrying in background")

        face_detector = mp.solutions.face_detection.FaceDetection(model_selection=0, min_detection_confidence=0.5)
        face_mesh = mp.solutions.face_mesh.FaceMesh(
            max_num_faces=2, refine_landmarks=False, min_detection_confidence=0.5, min_tracking_confidence=0.5
        )

        last_process_time = 0.0
        last_retry_time = 0.0
        try:
            while not self._stop_event.is_set():
                if self._cap is None or not self._cap.isOpened():
                    now = time.monotonic()
                    if now - last_retry_time >= CAMERA_RETRY_INTERVAL_SECONDS:
                        last_retry_time = now
                        if self._cap is not None:
                            self._cap.release()
                        self._cap = cv2.VideoCapture(self.config.camera_index)
                        if self._cap.isOpened():
                            logger.info("camera became available again")
                            self._camera_unavailable_reported = False
                            self._on_state_change("ACTIVE")
                    time.sleep(0.5)
                    continue

                ok, frame = self._cap.read()
                if not ok or frame is None:
                    self._consecutive_read_failures += 1
                    if (
                        self._consecutive_read_failures >= CONSECUTIVE_READ_FAILURES_BEFORE_ALERT
                        and not self._camera_unavailable_reported
                    ):
                        self._camera_unavailable_reported = True
                        self._emit(
                            "CAMERA_UNAVAILABLE",
                            {"consecutive_failed_frames": self._consecutive_read_failures},
                            Severity.YELLOW,
                        )
                        self._on_state_change("UNAVAILABLE")
                    time.sleep(0.2)
                    continue

                self._consecutive_read_failures = 0
                if self._camera_unavailable_reported:
                    self._camera_unavailable_reported = False
                    self._on_state_change("ACTIVE")

                with self._frame_lock:
                    self._last_frame = frame

                now = time.monotonic()
                if now - last_process_time < self.config.frame_interval_seconds:
                    continue
                last_process_time = now

                self._process_frame(frame, face_detector, face_mesh)
        finally:
            face_detector.close()
            face_mesh.close()
            if self._cap is not None:
                self._cap.release()

    def _process_frame(self, frame, face_detector, face_mesh) -> None:
        import cv2

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = frame.shape[:2]
        now = time.monotonic()

        detection_result = face_detector.process(rgb)
        faces = detection_result.detections or []
        face_count = len(faces)

        if face_count == 0:
            self._multi_face_since = None
            self._face_away_since = None
            self._look_away_since = None
            self._on_state_change("NO_FACE")
            if self._no_face_since is None:
                self._no_face_since = now
                self._prolonged_absence_reported = False
            elapsed = now - self._no_face_since
            if elapsed >= self.config.prolonged_absence_threshold_seconds and not self._prolonged_absence_reported:
                self._prolonged_absence_reported = True
                self._emit("POSSIBLE_PROLONGED_ABSENCE", {"duration_seconds": round(elapsed, 1)}, Severity.RED)
            elif elapsed >= self.config.no_face_threshold_seconds:
                self._emit("NO_FACE_DETECTED", {"duration_seconds": round(elapsed, 1)})
            return

        self._no_face_since = None
        self._prolonged_absence_reported = False

        if face_count > 1:
            self._face_away_since = None
            self._look_away_since = None
            self._on_state_change("MULTIPLE_FACES")
            if self._multi_face_since is None:
                self._multi_face_since = now
            elapsed = now - self._multi_face_since
            if elapsed >= self.config.multiple_face_threshold_seconds:
                self._emit("MULTIPLE_FACES_DETECTED", {"face_count": face_count, "duration_seconds": round(elapsed, 1)})
            return

        self._multi_face_since = None

        mesh_result = face_mesh.process(rgb)
        if not mesh_result.multi_face_landmarks:
            self._look_away_since = None
            self._on_state_change("FACE_ANGLE_UNCLEAR")
            if self._face_away_since is None:
                self._face_away_since = now
            elapsed = now - self._face_away_since
            if elapsed >= self.config.face_away_threshold_seconds:
                self._emit("FACE_AWAY", {"duration_seconds": round(elapsed, 1)})
            return

        self._face_away_since = None
        landmarks = mesh_result.multi_face_landmarks[0].landmark
        pose = _estimate_head_pose(landmarks, w, h)

        if pose is None:
            self._on_state_change("ONE_FACE")
            self._look_away_since = None
            return

        direction, yaw_deg, pitch_deg = pose
        if direction == "center":
            self._on_state_change("ONE_FACE")
            self._look_away_since = None
            self._look_away_direction = None
            return

        self._on_state_change(f"LOOKING_{direction.upper()}")
        if self._look_away_direction != direction:
            self._look_away_direction = direction
            self._look_away_since = now
            return

        elapsed = now - self._look_away_since
        if elapsed >= self.config.look_away_threshold_seconds:
            self._emit(
                "PROLONGED_LOOK_AWAY",
                {"direction": direction, "duration_seconds": round(elapsed, 1), "yaw_deg": round(yaw_deg, 1), "pitch_deg": round(pitch_deg, 1)},
            )
