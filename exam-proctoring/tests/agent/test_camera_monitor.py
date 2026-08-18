"""CameraMonitor threshold/state-machine logic, using mocked MediaPipe results.

No physical webcam or real inference is used: `_process_frame` is exercised
directly with a throwaway numpy frame and fake detector/mesh objects that
mimic MediaPipe's result shape, per the spec's requirement that camera logic
be testable without hardware.
"""
from __future__ import annotations

import time

import numpy as np
import pytest


class FakeDetectionResult:
    def __init__(self, num_faces):
        self.detections = [object()] * num_faces


class FakeLandmarkContainer:
    def __init__(self):
        self.landmark = []


class FakeMeshResult:
    def __init__(self, num_faces):
        self.multi_face_landmarks = [FakeLandmarkContainer() for _ in range(num_faces)]


class FakeDetector:
    def __init__(self, face_count):
        self.face_count = face_count

    def process(self, rgb):
        return FakeDetectionResult(self.face_count)


class FakeMesh:
    def __init__(self, num_faces):
        self.num_faces = num_faces

    def process(self, rgb):
        return FakeMeshResult(self.num_faces)


def _make_monitor(no_face=0.03, multi_face=0.03, face_away=0.03, absence=0.08, look_away=0.03):
    from monitors.camera_monitor import CameraMonitor, CameraMonitorConfig

    signals = []
    states = []
    config = CameraMonitorConfig(
        camera_index=0,
        frame_interval_seconds=0.0,
        no_face_threshold_seconds=no_face,
        multiple_face_threshold_seconds=multi_face,
        face_away_threshold_seconds=face_away,
        prolonged_absence_threshold_seconds=absence,
        look_away_threshold_seconds=look_away,
    )
    monitor = CameraMonitor(config, on_signal=lambda t, m, s: signals.append((t, m, s)), on_state_change=states.append)
    return monitor, signals, states


def _frame():
    return np.zeros((10, 10, 3), dtype=np.uint8)


def test_no_face_detected_signal_after_threshold():
    monitor, signals, states = _make_monitor(no_face=0.02)
    detector, mesh = FakeDetector(0), FakeMesh(0)

    monitor._process_frame(_frame(), detector, mesh)  # starts the no-face timer, too soon to fire
    assert not any(s[0] == "NO_FACE_DETECTED" for s in signals)

    time.sleep(0.03)
    monitor._process_frame(_frame(), detector, mesh)
    assert any(s[0] == "NO_FACE_DETECTED" for s in signals)
    assert states[-1] == "NO_FACE"


def test_prolonged_absence_fires_once_and_supersedes_no_face():
    monitor, signals, states = _make_monitor(no_face=0.01, absence=0.05)
    detector, mesh = FakeDetector(0), FakeMesh(0)

    monitor._process_frame(_frame(), detector, mesh)
    time.sleep(0.02)
    monitor._process_frame(_frame(), detector, mesh)  # crosses no_face threshold
    time.sleep(0.06)
    monitor._process_frame(_frame(), detector, mesh)  # crosses prolonged absence threshold

    absence_signals = [s for s in signals if s[0] == "POSSIBLE_PROLONGED_ABSENCE"]
    assert len(absence_signals) == 1
    assert absence_signals[0][2].value == "red"

    # calling again should not re-fire prolonged absence (reported flag)
    time.sleep(0.02)
    monitor._process_frame(_frame(), detector, mesh)
    assert len([s for s in signals if s[0] == "POSSIBLE_PROLONGED_ABSENCE"]) == 1


def test_multiple_faces_detected_after_threshold():
    monitor, signals, states = _make_monitor(multi_face=0.02)
    detector, mesh = FakeDetector(2), FakeMesh(2)

    monitor._process_frame(_frame(), detector, mesh)
    time.sleep(0.03)
    monitor._process_frame(_frame(), detector, mesh)

    matching = [s for s in signals if s[0] == "MULTIPLE_FACES_DETECTED"]
    assert len(matching) == 1
    assert matching[0][1]["face_count"] == 2
    assert states[-1] == "MULTIPLE_FACES"


def test_face_recovery_resets_no_face_timer():
    monitor, signals, states = _make_monitor(no_face=0.02)
    detector_absent, mesh_absent = FakeDetector(0), FakeMesh(0)
    detector_present, mesh_present = FakeDetector(1), FakeMesh(1)

    monitor._process_frame(_frame(), detector_absent, mesh_absent)
    time.sleep(0.03)
    monitor._process_frame(_frame(), detector_absent, mesh_absent)
    assert any(s[0] == "NO_FACE_DETECTED" for s in signals)

    signals.clear()
    # face reappears with no mesh landmarks -> FACE_ANGLE_UNCLEAR path, not NO_FACE_DETECTED
    monitor._process_frame(_frame(), detector_present, FakeMesh(0))
    assert monitor._no_face_since is None
    assert not any(s[0] == "NO_FACE_DETECTED" for s in signals)


def test_face_away_when_mesh_landmarks_unavailable():
    monitor, signals, states = _make_monitor(face_away=0.02)
    detector = FakeDetector(1)
    mesh_no_landmarks = FakeMesh(0)  # face bbox present, but mesh can't resolve landmarks

    monitor._process_frame(_frame(), detector, mesh_no_landmarks)
    time.sleep(0.03)
    monitor._process_frame(_frame(), detector, mesh_no_landmarks)

    assert any(s[0] == "FACE_AWAY" for s in signals)


def test_prolonged_look_away_uses_estimated_head_pose(monkeypatch):
    import monitors.camera_monitor as camera_monitor_module

    monkeypatch.setattr(camera_monitor_module, "_estimate_head_pose", lambda landmarks, w, h: ("left", -25.0, 2.0))

    monitor, signals, states = _make_monitor(look_away=0.02)
    detector, mesh = FakeDetector(1), FakeMesh(1)

    monitor._process_frame(_frame(), detector, mesh)  # direction changes to "left", starts timer
    assert not any(s[0] == "PROLONGED_LOOK_AWAY" for s in signals)

    time.sleep(0.03)
    monitor._process_frame(_frame(), detector, mesh)

    matching = [s for s in signals if s[0] == "PROLONGED_LOOK_AWAY"]
    assert len(matching) == 1
    assert matching[0][1]["direction"] == "left"
    assert states[-1] == "LOOKING_LEFT"


def test_centered_gaze_produces_no_look_away_signal(monkeypatch):
    import monitors.camera_monitor as camera_monitor_module

    monkeypatch.setattr(camera_monitor_module, "_estimate_head_pose", lambda landmarks, w, h: ("center", 2.0, 1.0))

    monitor, signals, states = _make_monitor(look_away=0.01)
    detector, mesh = FakeDetector(1), FakeMesh(1)

    monitor._process_frame(_frame(), detector, mesh)
    time.sleep(0.02)
    monitor._process_frame(_frame(), detector, mesh)

    assert not any(s[0] == "PROLONGED_LOOK_AWAY" for s in signals)
    assert states[-1] == "ONE_FACE"
