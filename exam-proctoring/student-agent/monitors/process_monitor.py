"""Detects known remote-access and virtual-camera tools running on the machine.

This targets the two most common *software-based* ways a student could
undermine the other checks: someone else remotely controlling/watching the
machine (TeamViewer, AnyDesk, VNC, ...), or a virtual-camera driver feeding
a fake image into the webcam instead of the real feed (OBS Virtual Camera,
Snap Camera, ManyCam, ...). Both categories default to RED immediately
(not the usual "3 strikes" escalation) because neither has a legitimate
reason to be running during a proctored session.

Scope and method, matching the rest of this codebase's constraints: this
only enumerates process *names* (exactly what `ps`/Activity Monitor/Task
Manager show any user, no elevated privileges, no window contents, no
memory inspection) and checks them against a configurable keyword list. It
does not kill/block anything - detection only.

HARD LIMITATION: this can only ever see software running *on this machine*.
A second physical device (e.g. a phone simply pointed at the screen, or
used for a voice call to relay answers) leaves no process for this - or any
client-side tool - to find. See README "Technical limitations".

Common video-calling/collaboration apps (Zoom, Teams, Discord, Meet) are
deliberately NOT in the default blocklists: a study group may legitimately
want a call running alongside the exam, and defaulting to flagging them
would just be noise. Add them to the config lists if your group wants that.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("agent.monitors.process")


@dataclass
class ProcessScanResult:
    remote_access_matches: list[str] = field(default_factory=list)
    virtual_camera_matches: list[str] = field(default_factory=list)
    available: bool = True
    error_message: Optional[str] = None


class ProcessMonitor:
    def __init__(self, remote_access_keywords: tuple[str, ...], virtual_camera_keywords: tuple[str, ...]) -> None:
        self._remote_access_keywords = [k.lower() for k in remote_access_keywords if k.strip()]
        self._virtual_camera_keywords = [k.lower() for k in virtual_camera_keywords if k.strip()]

    def scan(self) -> ProcessScanResult:
        try:
            import psutil
        except ImportError as exc:
            return ProcessScanResult(available=False, error_message=f"psutil not installed: {exc}")

        remote_matches: set[str] = set()
        camera_matches: set[str] = set()
        try:
            for proc in psutil.process_iter(["name"]):
                name = (proc.info.get("name") or "").lower()
                if not name:
                    continue
                for keyword in self._remote_access_keywords:
                    if keyword in name:
                        remote_matches.add(name)
                for keyword in self._virtual_camera_keywords:
                    if keyword in name:
                        camera_matches.add(name)
        except Exception as exc:  # e.g. transient permission errors enumerating some process
            return ProcessScanResult(available=False, error_message=f"process scan failed: {exc}")

        return ProcessScanResult(sorted(remote_matches), sorted(camera_matches), available=True)


class ProcessActivityTracker:
    """Emits a signal for every scan that finds a match (re-signaling while a
    tool keeps running, not just once), and leaves rate-limiting entirely to
    the EventEngine's cooldown - there's no "changed vs. persisted" split to
    make here the way there is for window/display state.
    """

    def __init__(self) -> None:
        self._unavailable_reported = False

    def observe(self, result: ProcessScanResult) -> list[tuple[str, dict]]:
        signals: list[tuple[str, dict]] = []

        if not result.available:
            if not self._unavailable_reported:
                self._unavailable_reported = True
                signals.append(("PROCESS_MONITOR_UNAVAILABLE", {"reason": result.error_message}))
            return signals
        self._unavailable_reported = False

        if result.remote_access_matches:
            signals.append(("REMOTE_ACCESS_TOOL_DETECTED", {"processes": result.remote_access_matches}))
        if result.virtual_camera_matches:
            signals.append(("VIRTUAL_CAMERA_TOOL_DETECTED", {"processes": result.virtual_camera_matches}))

        return signals
