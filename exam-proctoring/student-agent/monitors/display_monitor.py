"""Display configuration monitoring: detects multiple displays and mirrored
display configurations reported by the OS.

IMPORTANT LIMITATIONS (see also README "Technical Limitations"):
  - This is detection of the *local machine's own* display configuration only.
    It never inspects, connects to, or infers the contents of another
    person's device or display.
  - It does not disable mirroring, and it cannot determine *what* is being
    shown on a mirrored/external display, only that such a configuration is
    reported by the OS.
  - Display mirroring detection does not, by itself, prove a student used a
    second device to view exam content; it is a signal for human review.
"""
from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("agent.monitors.display")


@dataclass
class DisplayState:
    display_count: int
    mirrored: bool
    extended: bool
    available: bool
    error_message: Optional[str] = None
    raw: dict = field(default_factory=dict)


class DisplayMonitor(ABC):
    @abstractmethod
    def get_display_state(self) -> DisplayState:
        ...

    def start(self) -> None:  # noqa: B027
        pass

    def stop(self) -> None:  # noqa: B027
        pass


class MacDisplayMonitor(DisplayMonitor):
    """Uses public CoreGraphics display APIs (via pyobjc's Quartz module):
    CGGetOnlineDisplayList to enumerate displays, and CGDisplayMirrorsDisplay /
    CGDisplayIsInMirrorSet to determine mirroring — the direct CoreGraphics
    equivalents of the APIs named in the spec (CGDisplayIsMirrorOfDisplay /
    CGGetOnlineDisplayList).
    """

    def __init__(self) -> None:
        self._quartz = None
        self._error: Optional[str] = None
        try:
            import Quartz  # type: ignore

            self._quartz = Quartz
        except Exception as exc:
            self._error = (
                f"macOS display monitoring unavailable: pyobjc (Quartz) is not installed: {exc}. "
                "Run `pip install pyobjc-framework-Quartz`."
            )

    def get_display_state(self) -> DisplayState:
        if self._quartz is None:
            return DisplayState(0, False, False, False, self._error)
        try:
            max_displays = 16
            err, display_ids, count = self._quartz.CGGetOnlineDisplayList(max_displays, None, None)
            if err != 0:
                return DisplayState(0, False, False, False, f"CGGetOnlineDisplayList failed (CGError {err})")

            display_ids = list(display_ids)[:count]
            mirrored_pairs = []
            for display_id in display_ids:
                mirrors_id = self._quartz.CGDisplayMirrorsDisplay(display_id)
                if mirrors_id != 0:
                    mirrored_pairs.append((display_id, mirrors_id))

            mirrored = len(mirrored_pairs) > 0
            display_count = len(display_ids)
            extended = display_count > 1 and not mirrored

            return DisplayState(
                display_count=display_count,
                mirrored=mirrored,
                extended=extended,
                available=True,
                raw={"display_ids": display_ids, "mirrored_pairs": mirrored_pairs},
            )
        except Exception as exc:
            return DisplayState(0, False, False, False, f"failed to query display configuration: {exc}")


class WindowsDisplayMonitor(DisplayMonitor):
    """Best-effort: monitor count via GetSystemMetrics(SM_CMONITORS), topology
    (clone vs. extend) via the documented QueryDisplayConfig WinAPI. If the
    ctypes calls fail on a given Windows build, falls back to reporting the
    monitor count with mirroring left undetermined rather than guessing.
    """

    SM_CMONITORS = 80
    QDC_ONLY_ACTIVE_PATHS = 0x00000002
    ERROR_SUCCESS = 0

    def __init__(self) -> None:
        self._error: Optional[str] = None
        try:
            import ctypes  # noqa: F401

            self._ctypes = __import__("ctypes")
        except Exception as exc:
            self._error = f"Windows display monitoring unavailable: {exc}"

    def get_display_state(self) -> DisplayState:
        if self._error:
            return DisplayState(0, False, False, False, self._error)
        ctypes = self._ctypes
        try:
            user32 = ctypes.windll.user32
            display_count = user32.GetSystemMetrics(self.SM_CMONITORS)
        except Exception as exc:
            return DisplayState(0, False, False, False, f"failed to read monitor count: {exc}")

        # Clone (mirrored) vs. extend topology on Windows can only be told
        # apart reliably via QueryDisplayConfig's DISPLAYCONFIG_PATH_INFO
        # array (comparing source ids across active paths), which needs
        # struct marshalling this module doesn't implement. A prior version
        # of this code called GetDisplayConfigBufferSizes and treated mere
        # success as "topology known" - but that call only reports buffer
        # sizes, it doesn't determine clone-vs-extend, so it was marking
        # topology "known" (and mirrored confidently False) on virtually
        # every Windows build regardless of actual configuration. Being
        # honest about not knowing is safer than a confident wrong answer.
        mirrored = False
        topology_known = False

        extended = display_count > 1 and not mirrored
        note = None if topology_known else (
            "monitor count detected, but exact clone-vs-extend topology could not be "
            "determined on this Windows build; treating multiple monitors as extended "
            "rather than assuming mirroring is absent"
        )
        return DisplayState(
            display_count=display_count,
            mirrored=mirrored,
            extended=extended,
            available=True,
            error_message=note,
            raw={"topology_known": topology_known},
        )


class LinuxDisplayMonitor(DisplayMonitor):
    """Uses `xrandr --query` (X11) to enumerate connected outputs and infer
    mirroring from identical output geometries. Not available under Wayland-only
    sessions or headless environments without xrandr.
    """

    _GEOMETRY_RE = re.compile(r"connected(?:\s+primary)?\s+(\d+)x(\d+)\+(\d+)\+(\d+)")

    def __init__(self) -> None:
        self._xrandr = shutil.which("xrandr")

    def get_display_state(self) -> DisplayState:
        if not self._xrandr:
            return DisplayState(
                0, False, False, False,
                "Display monitoring unavailable: `xrandr` is not installed or this is a "
                "non-X11 (Wayland) session. Install xrandr / run under Xorg to enable this.",
            )
        try:
            result = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, timeout=3)
            if result.returncode != 0:
                return DisplayState(0, False, False, False, f"xrandr exited with code {result.returncode}")

            geometries = []
            for line in result.stdout.splitlines():
                if " connected" not in line:
                    continue
                match = self._GEOMETRY_RE.search(line)
                if match:
                    w, h, x, y = match.groups()
                    geometries.append((w, h, x, y))

            display_count = len(geometries)
            mirrored = display_count > 1 and len(set(geometries)) < display_count
            extended = display_count > 1 and not mirrored

            return DisplayState(
                display_count=display_count, mirrored=mirrored, extended=extended,
                available=True, raw={"geometries": geometries},
            )
        except subprocess.TimeoutExpired:
            return DisplayState(0, False, False, False, "xrandr timed out")
        except Exception as exc:
            return DisplayState(0, False, False, False, f"failed to query display configuration: {exc}")


class UnsupportedDisplayMonitor(DisplayMonitor):
    def __init__(self, os_name: str) -> None:
        self._os_name = os_name

    def get_display_state(self) -> DisplayState:
        return DisplayState(0, False, False, False, f"Display monitoring is not implemented for platform '{self._os_name}'.")


def get_display_monitor() -> DisplayMonitor:
    system = platform.system()
    if system == "Darwin":
        return MacDisplayMonitor()
    if system == "Windows":
        return WindowsDisplayMonitor()
    if system == "Linux":
        return LinuxDisplayMonitor()
    return UnsupportedDisplayMonitor(system)


class DisplayActivityTracker:
    """Turns successive DisplayState reads into signals, distinguishing extended
    vs. mirrored vs. multi-display vs. a bare configuration change.
    """

    def __init__(self) -> None:
        self._last: Optional[DisplayState] = None
        self._unavailable_reported = False

    def observe(self, state: DisplayState) -> list[tuple[str, dict]]:
        signals: list[tuple[str, dict]] = []

        if not state.available:
            if not self._unavailable_reported:
                self._unavailable_reported = True
                signals.append(("DISPLAY_MONITOR_UNAVAILABLE", {"reason": state.error_message}))
            return signals
        self._unavailable_reported = False

        previous = self._last
        self._last = state

        metadata = {
            "display_count": state.display_count,
            "mirrored": state.mirrored,
            "extended": state.extended,
        }

        if previous is None:
            if state.mirrored:
                signals.append(("DISPLAY_MIRRORING_DETECTED", metadata))
            elif state.display_count > 1:
                signals.append(("EXTERNAL_DISPLAY_DETECTED", metadata))
            return signals

        changed = (
            previous.display_count != state.display_count
            or previous.mirrored != state.mirrored
            or previous.extended != state.extended
        )
        if not changed:
            # Still re-signal an ongoing mirrored/multi-display condition periodically so
            # the EventEngine's cooldown/escalation logic can turn persistence into RED.
            if state.mirrored:
                signals.append(("DISPLAY_MIRRORING_DETECTED", metadata))
            elif state.display_count > 1:
                signals.append(("EXTERNAL_DISPLAY_DETECTED", metadata))
            return signals

        signals.append(("DISPLAY_CONFIGURATION_CHANGED", {**metadata, "previous_display_count": previous.display_count}))
        if state.mirrored and not previous.mirrored:
            signals.append(("DISPLAY_MIRRORING_DETECTED", metadata))
        elif state.display_count > 1 and previous.display_count <= 1:
            signals.append(("EXTERNAL_DISPLAY_DETECTED", metadata))

        return signals
