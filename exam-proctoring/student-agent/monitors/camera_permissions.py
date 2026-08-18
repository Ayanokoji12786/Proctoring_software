"""macOS camera permission handling.

OpenCV's own AVFoundation authorization trigger is asynchronous and nothing
waits for the user's decision: it fires the system prompt and returns
immediately with "not authorized," regardless of whether the prompt is still
sitting on screen unanswered. That's true even from the main thread. This
module talks to AVFoundation directly (via pyobjc) and pumps the run loop
until the user actually responds (or a timeout elapses), so by the time
CameraMonitor's background thread opens the camera, authorization is fully
resolved one way or the other.

Must be called from the main thread, before any other thread touches the
camera - macOS can only present the system permission dialog from the main
thread's run loop (this is also why CameraMonitor itself can't do this from
its own background thread).
"""
from __future__ import annotations

import logging
import platform
import time

logger = logging.getLogger("agent.monitors.camera_permissions")

_AUTHORIZED = 3
_NOT_DETERMINED = 0
_DENIED = 2
_RESTRICTED = 1


def ensure_camera_authorized(timeout_seconds: float = 45.0) -> bool:
    """Returns True if the camera is (now) authorized for this process.

    No-op (returns True) on non-macOS platforms - Windows/Linux don't share
    this specific "async request nobody waits for" failure mode.
    """
    if platform.system() != "Darwin":
        return True

    try:
        import AVFoundation
        from Foundation import NSDate, NSRunLoop
    except ImportError as exc:
        logger.warning("pyobjc AVFoundation not installed, cannot pre-check camera permission: %s", exc)
        return True  # fall through and let OpenCV's own (imperfect) handling try

    status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeVideo)

    if status == _AUTHORIZED:
        return True
    if status in (_DENIED, _RESTRICTED):
        logger.warning(
            "camera access previously denied/restricted for this app - camera monitoring will stay "
            "unavailable until enabled manually in System Settings > Privacy & Security > Camera"
        )
        return False
    if status != _NOT_DETERMINED:
        return False

    logger.info("requesting camera permission - waiting for you to respond to the system dialog")
    result = {"done": False, "granted": False}

    def _completion_handler(granted: bool) -> None:
        result["granted"] = bool(granted)
        result["done"] = True

    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
        AVFoundation.AVMediaTypeVideo, _completion_handler
    )

    # The completion handler is delivered asynchronously; it will never fire
    # unless we actively pump the run loop ourselves while we wait for it.
    deadline = time.monotonic() + timeout_seconds
    run_loop = NSRunLoop.currentRunLoop()
    while not result["done"] and time.monotonic() < deadline:
        run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))

    if not result["done"]:
        logger.warning("timed out waiting for camera permission response after %.0fs", timeout_seconds)
        return False

    logger.info("camera permission %s", "granted" if result["granted"] else "denied")
    return result["granted"]
