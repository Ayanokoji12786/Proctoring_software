"""ensure_camera_authorized(): status handling and the async-wait logic,
against a fully mocked AVFoundation/Foundation bridge (no real macOS
permission dialog involved).
"""
from __future__ import annotations

import sys
import types


class FakeAVCaptureDevice:
    def __init__(self, status, on_request=None):
        self._status = status
        self._on_request = on_request

    def authorizationStatusForMediaType_(self, media_type):
        return self._status

    def requestAccessForMediaType_completionHandler_(self, media_type, handler):
        if self._on_request:
            self._on_request(handler)


class FakeNSDate:
    @staticmethod
    def dateWithTimeIntervalSinceNow_(seconds):
        return object()


class FakeNSRunLoop:
    def __init__(self, on_run=None):
        self._on_run = on_run

    def runUntilDate_(self, date):
        if self._on_run:
            self._on_run()

    @classmethod
    def currentRunLoop(cls):
        return cls._instance


def _install_fake_avfoundation(monkeypatch, status, on_request=None, run_loop_on_run=None):
    fake_avfoundation = types.ModuleType("AVFoundation")
    fake_avfoundation.AVMediaTypeVideo = "video"
    fake_avfoundation.AVCaptureDevice = FakeAVCaptureDevice(status, on_request)

    fake_foundation = types.ModuleType("Foundation")
    fake_foundation.NSDate = FakeNSDate
    run_loop_cls = FakeNSRunLoop
    run_loop_cls._instance = FakeNSRunLoop(run_loop_on_run)
    fake_foundation.NSRunLoop = run_loop_cls

    monkeypatch.setitem(sys.modules, "AVFoundation", fake_avfoundation)
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)


def test_noop_true_on_non_macos(monkeypatch):
    import monitors.camera_permissions as camera_permissions

    monkeypatch.setattr(camera_permissions.platform, "system", lambda: "Linux")
    assert camera_permissions.ensure_camera_authorized() is True


def test_already_authorized_returns_true_without_prompting(monkeypatch):
    import monitors.camera_permissions as camera_permissions

    monkeypatch.setattr(camera_permissions.platform, "system", lambda: "Darwin")
    _install_fake_avfoundation(monkeypatch, status=camera_permissions._AUTHORIZED)

    assert camera_permissions.ensure_camera_authorized() is True


def test_denied_returns_false_without_reprompting(monkeypatch):
    import monitors.camera_permissions as camera_permissions

    monkeypatch.setattr(camera_permissions.platform, "system", lambda: "Darwin")
    _install_fake_avfoundation(monkeypatch, status=camera_permissions._DENIED)

    assert camera_permissions.ensure_camera_authorized() is False


def test_restricted_returns_false(monkeypatch):
    import monitors.camera_permissions as camera_permissions

    monkeypatch.setattr(camera_permissions.platform, "system", lambda: "Darwin")
    _install_fake_avfoundation(monkeypatch, status=camera_permissions._RESTRICTED)

    assert camera_permissions.ensure_camera_authorized() is False


def test_not_determined_waits_for_completion_handler_then_returns_true(monkeypatch):
    import monitors.camera_permissions as camera_permissions

    monkeypatch.setattr(camera_permissions.platform, "system", lambda: "Darwin")

    captured_handler = {}

    def on_request(handler):
        captured_handler["fn"] = handler

    call_count = {"n": 0}

    def on_run():
        # Simulate the OS delivering the async completion callback after a
        # couple of run-loop pumps, exactly like a real user's click would.
        call_count["n"] += 1
        if call_count["n"] >= 2:
            captured_handler["fn"](True)

    _install_fake_avfoundation(
        monkeypatch, status=camera_permissions._NOT_DETERMINED, on_request=on_request, run_loop_on_run=on_run
    )

    assert camera_permissions.ensure_camera_authorized(timeout_seconds=5) is True


def test_not_determined_user_denies_returns_false(monkeypatch):
    import monitors.camera_permissions as camera_permissions

    monkeypatch.setattr(camera_permissions.platform, "system", lambda: "Darwin")

    captured_handler = {}

    def on_request(handler):
        captured_handler["fn"] = handler

    def on_run():
        captured_handler["fn"](False)

    _install_fake_avfoundation(
        monkeypatch, status=camera_permissions._NOT_DETERMINED, on_request=on_request, run_loop_on_run=on_run
    )

    assert camera_permissions.ensure_camera_authorized(timeout_seconds=5) is False


def test_not_determined_timeout_returns_false_if_never_answered(monkeypatch):
    import monitors.camera_permissions as camera_permissions

    monkeypatch.setattr(camera_permissions.platform, "system", lambda: "Darwin")

    # completion handler is never invoked - simulates the user ignoring the dialog
    _install_fake_avfoundation(monkeypatch, status=camera_permissions._NOT_DETERMINED, on_request=lambda h: None)

    assert camera_permissions.ensure_camera_authorized(timeout_seconds=0.3) is False


def test_missing_pyobjc_falls_back_to_true(monkeypatch):
    import builtins
    import monitors.camera_permissions as camera_permissions

    monkeypatch.setattr(camera_permissions.platform, "system", lambda: "Darwin")
    monkeypatch.delitem(sys.modules, "AVFoundation", raising=False)
    monkeypatch.delitem(sys.modules, "Foundation", raising=False)

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "AVFoundation":
            raise ImportError("no pyobjc-framework-AVFoundation installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert camera_permissions.ensure_camera_authorized() is True
