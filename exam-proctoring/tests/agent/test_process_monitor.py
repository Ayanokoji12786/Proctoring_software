"""ProcessMonitor / ProcessActivityTracker: blocklist matching against mocked psutil output."""
from __future__ import annotations


class _FakeProc:
    def __init__(self, name):
        self.info = {"name": name}


def _patch_processes(monkeypatch, names):
    import psutil

    def fake_process_iter(attrs=None):
        return [_FakeProc(n) for n in names]

    monkeypatch.setattr(psutil, "process_iter", fake_process_iter)


def test_scan_finds_no_matches_when_nothing_suspicious_running(monkeypatch):
    from monitors.process_monitor import ProcessMonitor

    _patch_processes(monkeypatch, ["Finder", "Terminal", "python3", "Notes"])
    monitor = ProcessMonitor(remote_access_keywords=("teamviewer", "anydesk"), virtual_camera_keywords=("obs", "snapcamera"))

    result = monitor.scan()

    assert result.available is True
    assert result.remote_access_matches == []
    assert result.virtual_camera_matches == []


def test_scan_detects_remote_access_tool_case_insensitively(monkeypatch):
    from monitors.process_monitor import ProcessMonitor

    _patch_processes(monkeypatch, ["Finder", "TeamViewer.exe", "python3"])
    monitor = ProcessMonitor(remote_access_keywords=("teamviewer",), virtual_camera_keywords=("obs",))

    result = monitor.scan()

    assert result.remote_access_matches == ["teamviewer.exe"]
    assert result.virtual_camera_matches == []


def test_scan_detects_virtual_camera_tool(monkeypatch):
    from monitors.process_monitor import ProcessMonitor

    _patch_processes(monkeypatch, ["obs64.exe", "chrome.exe"])
    monitor = ProcessMonitor(remote_access_keywords=("teamviewer",), virtual_camera_keywords=("obs64", "snapcamera"))

    result = monitor.scan()

    assert result.remote_access_matches == []
    assert result.virtual_camera_matches == ["obs64.exe"]


def test_scan_detects_both_categories_simultaneously(monkeypatch):
    from monitors.process_monitor import ProcessMonitor

    _patch_processes(monkeypatch, ["anydesk", "manycam", "Finder"])
    monitor = ProcessMonitor(remote_access_keywords=("anydesk",), virtual_camera_keywords=("manycam",))

    result = monitor.scan()

    assert result.remote_access_matches == ["anydesk"]
    assert result.virtual_camera_matches == ["manycam"]


def test_empty_keyword_lists_never_match_anything(monkeypatch):
    from monitors.process_monitor import ProcessMonitor

    _patch_processes(monkeypatch, ["teamviewer", "obs64"])
    monitor = ProcessMonitor(remote_access_keywords=(), virtual_camera_keywords=())

    result = monitor.scan()

    assert result.remote_access_matches == []
    assert result.virtual_camera_matches == []


def test_tracker_emits_signal_for_each_category_present():
    from monitors.process_monitor import ProcessActivityTracker, ProcessScanResult

    tracker = ProcessActivityTracker()
    result = ProcessScanResult(remote_access_matches=["teamviewer"], virtual_camera_matches=["obs64"], available=True)

    signals = tracker.observe(result)

    types = {s[0] for s in signals}
    assert types == {"REMOTE_ACCESS_TOOL_DETECTED", "VIRTUAL_CAMERA_TOOL_DETECTED"}
    remote_signal = next(s for s in signals if s[0] == "REMOTE_ACCESS_TOOL_DETECTED")
    assert remote_signal[1] == {"processes": ["teamviewer"]}


def test_tracker_emits_nothing_when_clean():
    from monitors.process_monitor import ProcessActivityTracker, ProcessScanResult

    tracker = ProcessActivityTracker()
    signals = tracker.observe(ProcessScanResult(available=True))
    assert signals == []


def test_tracker_re_signals_while_tool_persists():
    from monitors.process_monitor import ProcessActivityTracker, ProcessScanResult

    tracker = ProcessActivityTracker()
    result = ProcessScanResult(remote_access_matches=["anydesk"], available=True)

    first = tracker.observe(result)
    second = tracker.observe(result)  # still running on the next scan

    assert any(s[0] == "REMOTE_ACCESS_TOOL_DETECTED" for s in first)
    assert any(s[0] == "REMOTE_ACCESS_TOOL_DETECTED" for s in second)


def test_tracker_unavailable_reports_once():
    from monitors.process_monitor import ProcessActivityTracker, ProcessScanResult

    tracker = ProcessActivityTracker()
    unavailable = ProcessScanResult(available=False, error_message="psutil not installed")

    first = tracker.observe(unavailable)
    second = tracker.observe(unavailable)

    assert first == [("PROCESS_MONITOR_UNAVAILABLE", {"reason": "psutil not installed"})]
    assert second == []


def test_default_severity_is_red_for_bypass_events():
    from events.schemas import DEFAULT_SEVERITY, EventType, Severity

    assert DEFAULT_SEVERITY[EventType.REMOTE_ACCESS_TOOL_DETECTED] == Severity.RED
    assert DEFAULT_SEVERITY[EventType.VIRTUAL_CAMERA_TOOL_DETECTED] == Severity.RED


def test_event_engine_emits_red_immediately_no_escalation_needed(agent_config):
    from events.engine import EventEngine
    from events.schemas import EventType, Signal

    emitted = []
    engine = EventEngine(agent_config.session_id, agent_config.student_id, agent_config, on_emit=emitted.append)

    signal = Signal(source="process", event_type=EventType.REMOTE_ACCESS_TOOL_DETECTED, metadata={"processes": ["teamviewer"]})
    event = engine.process_signal(signal)

    assert event is not None
    assert event.severity.value == "red"  # immediate red on first occurrence, unlike the yellow types
