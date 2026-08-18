"""DisplayActivityTracker: extended vs. mirrored vs. multi-display vs. config-change classification."""
from __future__ import annotations


def test_first_observation_single_display_produces_no_signal():
    from monitors.display_monitor import DisplayActivityTracker, DisplayState

    tracker = DisplayActivityTracker()
    signals = tracker.observe(DisplayState(display_count=1, mirrored=False, extended=False, available=True))
    assert signals == []


def test_first_observation_mirrored_flags_immediately():
    from monitors.display_monitor import DisplayActivityTracker, DisplayState

    tracker = DisplayActivityTracker()
    signals = tracker.observe(DisplayState(display_count=2, mirrored=True, extended=False, available=True))
    assert ("DISPLAY_MIRRORING_DETECTED", {"display_count": 2, "mirrored": True, "extended": False}) in signals


def test_transition_to_extended_display_flags_external_display():
    from monitors.display_monitor import DisplayActivityTracker, DisplayState

    tracker = DisplayActivityTracker()
    tracker.observe(DisplayState(display_count=1, mirrored=False, extended=False, available=True))
    signals = tracker.observe(DisplayState(display_count=2, mirrored=False, extended=True, available=True))

    types = [s[0] for s in signals]
    assert "DISPLAY_CONFIGURATION_CHANGED" in types
    assert "EXTERNAL_DISPLAY_DETECTED" in types
    assert "DISPLAY_MIRRORING_DETECTED" not in types


def test_transition_to_mirrored_flags_mirroring_not_external():
    from monitors.display_monitor import DisplayActivityTracker, DisplayState

    tracker = DisplayActivityTracker()
    tracker.observe(DisplayState(display_count=1, mirrored=False, extended=False, available=True))
    signals = tracker.observe(DisplayState(display_count=2, mirrored=True, extended=False, available=True))

    types = [s[0] for s in signals]
    assert "DISPLAY_CONFIGURATION_CHANGED" in types
    assert "DISPLAY_MIRRORING_DETECTED" in types


def test_persistent_mirroring_re_signals_for_engine_escalation():
    from monitors.display_monitor import DisplayActivityTracker, DisplayState

    tracker = DisplayActivityTracker()
    mirrored_state = DisplayState(display_count=2, mirrored=True, extended=False, available=True)
    tracker.observe(mirrored_state)  # first: flags immediately
    signals = tracker.observe(mirrored_state)  # unchanged, still mirrored

    assert ("DISPLAY_MIRRORING_DETECTED", {"display_count": 2, "mirrored": True, "extended": False}) in signals


def test_no_signal_when_nothing_changes_and_not_mirrored():
    from monitors.display_monitor import DisplayActivityTracker, DisplayState

    tracker = DisplayActivityTracker()
    state = DisplayState(display_count=1, mirrored=False, extended=False, available=True)
    tracker.observe(state)
    signals = tracker.observe(state)
    assert signals == []


def test_unavailable_reports_once():
    from monitors.display_monitor import DisplayActivityTracker, DisplayState

    tracker = DisplayActivityTracker()
    unavailable = DisplayState(0, False, False, available=False, error_message="xrandr not found")

    first = tracker.observe(unavailable)
    second = tracker.observe(unavailable)

    assert first == [("DISPLAY_MONITOR_UNAVAILABLE", {"reason": "xrandr not found"})]
    assert second == []
