"""Active window / application monitoring, behind a common cross-platform interface.

No single Python library reliably reports the active window on macOS, Windows,
and Linux, so each platform gets its own implementation. If a platform lacks
the permissions or tooling needed, `get_active_window()` returns an
`available=False` WindowInfo with a clear `error_message` instead of silently
returning nothing or crashing.
"""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("agent.monitors.window")

_BROWSER_PROCESS_NAMES = {
    "safari", "google chrome", "chrome", "firefox", "microsoft edge",
    "msedge", "brave browser", "opera", "arc",
}


@dataclass
class WindowInfo:
    application_name: Optional[str]
    window_title: Optional[str]
    available: bool
    error_message: Optional[str] = None


class WindowMonitor(ABC):
    """Common interface every platform-specific window monitor implements."""

    @abstractmethod
    def get_active_window(self) -> WindowInfo:
        ...

    def start(self) -> None:  # noqa: B027 - default no-op, platforms override if needed
        pass

    def stop(self) -> None:  # noqa: B027
        pass


class MacWindowMonitor(WindowMonitor):
    def __init__(self) -> None:
        self._workspace = None
        self._quartz = None
        self._import_error: Optional[str] = None
        try:
            from AppKit import NSWorkspace  # type: ignore

            self._workspace = NSWorkspace.sharedWorkspace()
        except Exception as exc:  # pyobjc not installed, or non-macOS
            self._import_error = f"AppKit unavailable: {exc}"
        try:
            import Quartz  # type: ignore

            self._quartz = Quartz
        except Exception:
            self._quartz = None  # window titles just won't be available; app name still works

    def get_active_window(self) -> WindowInfo:
        if self._workspace is None:
            return WindowInfo(
                None, None, False,
                "macOS window monitoring unavailable: pyobjc (AppKit) is not installed. "
                "Run `pip install pyobjc-framework-Cocoa`.",
            )
        try:
            app = self._workspace.frontmostApplication()
            app_name = str(app.localizedName()) if app else None
        except Exception as exc:
            return WindowInfo(None, None, False, f"failed to read frontmost application: {exc}")

        window_title = None
        if self._quartz is not None:
            try:
                window_list = self._quartz.CGWindowListCopyWindowInfo(
                    self._quartz.kCGWindowListOptionOnScreenOnly, self._quartz.kCGNullWindowID
                )
                for window in window_list:
                    if window.get("kCGWindowLayer") == 0 and window.get("kCGWindowOwnerName") == app_name:
                        window_title = window.get("kCGWindowName")
                        break
            except Exception:
                # Commonly caused by the app not having macOS Screen Recording permission
                # granted; app name (which needs no special permission) is still valid.
                window_title = None

        return WindowInfo(application_name=app_name, window_title=window_title, available=True)


class WindowsWindowMonitor(WindowMonitor):
    def __init__(self) -> None:
        self._error: Optional[str] = None
        try:
            import win32gui  # type: ignore
            import win32process  # type: ignore
            import psutil  # type: ignore

            self._win32gui = win32gui
            self._win32process = win32process
            self._psutil = psutil
        except Exception as exc:
            self._error = (
                f"Windows window monitoring unavailable: {exc}. "
                "Run `pip install pywin32 psutil`."
            )

    def get_active_window(self) -> WindowInfo:
        if self._error:
            return WindowInfo(None, None, False, self._error)
        try:
            hwnd = self._win32gui.GetForegroundWindow()
            if not hwnd:
                return WindowInfo(None, None, False, "no foreground window (desktop may be locked)")
            title = self._win32gui.GetWindowText(hwnd)
            _, pid = self._win32process.GetWindowThreadProcessId(hwnd)
            try:
                process = self._psutil.Process(pid)
                app_name = process.name()
            except Exception:
                app_name = None
            return WindowInfo(application_name=app_name, window_title=title or None, available=True)
        except Exception as exc:
            return WindowInfo(None, None, False, f"failed to read active window: {exc}")


class LinuxWindowMonitor(WindowMonitor):
    """Best-effort: relies on `xdotool` under X11. Wayland compositors generally do
    not expose the active window to unprivileged clients at all, so this clearly
    reports unavailability there rather than guessing.
    """

    def __init__(self) -> None:
        self._xdotool = shutil.which("xdotool")
        session_type = subprocess.os.environ.get("XDG_SESSION_TYPE", "")
        self._is_wayland = session_type.lower() == "wayland"

    def get_active_window(self) -> WindowInfo:
        if self._is_wayland:
            return WindowInfo(
                None, None, False,
                "Window monitoring is not available on Wayland sessions: Wayland does not "
                "expose the active window to ordinary applications. Run the exam under an "
                "X11 session for this feature.",
            )
        if not self._xdotool:
            return WindowInfo(
                None, None, False,
                "Window monitoring unavailable: `xdotool` is not installed. "
                "Install it (e.g. `sudo apt install xdotool`) to enable window monitoring.",
            )
        try:
            win_id = subprocess.run(
                ["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=2
            )
            if win_id.returncode != 0:
                return WindowInfo(None, None, False, "no active window (nothing focused)")
            window_id = win_id.stdout.strip()

            title_result = subprocess.run(
                ["xdotool", "getwindowname", window_id], capture_output=True, text=True, timeout=2
            )
            title = title_result.stdout.strip() or None

            pid_result = subprocess.run(
                ["xdotool", "getwindowpid", window_id], capture_output=True, text=True, timeout=2
            )
            app_name = None
            if pid_result.returncode == 0 and pid_result.stdout.strip():
                try:
                    import psutil  # type: ignore

                    app_name = psutil.Process(int(pid_result.stdout.strip())).name()
                except Exception:
                    app_name = None
            return WindowInfo(application_name=app_name, window_title=title, available=True)
        except subprocess.TimeoutExpired:
            return WindowInfo(None, None, False, "xdotool timed out")
        except Exception as exc:
            return WindowInfo(None, None, False, f"failed to read active window: {exc}")


class UnsupportedWindowMonitor(WindowMonitor):
    def __init__(self, os_name: str) -> None:
        self._os_name = os_name

    def get_active_window(self) -> WindowInfo:
        return WindowInfo(None, None, False, f"Window monitoring is not implemented for platform '{self._os_name}'.")


def get_window_monitor() -> WindowMonitor:
    system = platform.system()
    if system == "Darwin":
        return MacWindowMonitor()
    if system == "Windows":
        return WindowsWindowMonitor()
    if system == "Linux":
        return LinuxWindowMonitor()
    return UnsupportedWindowMonitor(system)


def is_browser(application_name: Optional[str]) -> bool:
    if not application_name:
        return False
    return application_name.strip().lower() in _BROWSER_PROCESS_NAMES


class WindowActivityTracker:
    """Consumes successive WindowInfo reads and decides which specific event type
    (if any) a change represents: BROWSER_FOCUS_LOST, APPLICATION_CHANGED, or the
    more general WINDOW_FOCUS_CHANGED (title-only change within the same app).
    """

    def __init__(self) -> None:
        self._last: Optional[WindowInfo] = None
        self._unavailable_reported = False

    def observe(self, info: WindowInfo) -> Optional[tuple[str, dict]]:
        """Returns (event_type_name, metadata) if this observation should produce a signal."""
        if not info.available:
            if not self._unavailable_reported:
                self._unavailable_reported = True
                return "WINDOW_MONITOR_UNAVAILABLE", {"reason": info.error_message}
            return None
        self._unavailable_reported = False

        previous = self._last
        self._last = info

        if previous is None:
            return None  # first observation establishes baseline only

        if previous.application_name == info.application_name and previous.window_title == info.window_title:
            return None  # no change

        metadata = {
            "previous_application": previous.application_name,
            "current_application": info.application_name,
            "previous_title": previous.window_title,
            "current_title": info.window_title,
        }

        if previous.application_name != info.application_name:
            if is_browser(previous.application_name) and not is_browser(info.application_name):
                return "BROWSER_FOCUS_LOST", metadata
            return "APPLICATION_CHANGED", metadata

        return "WINDOW_FOCUS_CHANGED", metadata
