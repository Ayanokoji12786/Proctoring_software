"""WindowActivityTracker: state-transition classification, without touching real OS window APIs."""
from __future__ import annotations


def test_first_observation_establishes_baseline_without_signal():
    from monitors.window_monitor import WindowActivityTracker, WindowInfo

    tracker = WindowActivityTracker()
    result = tracker.observe(WindowInfo("Exam App", "Exam Window", available=True))
    assert result is None


def test_no_change_produces_no_signal():
    from monitors.window_monitor import WindowActivityTracker, WindowInfo

    tracker = WindowActivityTracker()
    tracker.observe(WindowInfo("Exam App", "Exam Window", available=True))
    result = tracker.observe(WindowInfo("Exam App", "Exam Window", available=True))
    assert result is None


def test_title_only_change_is_window_focus_changed():
    from monitors.window_monitor import WindowActivityTracker, WindowInfo

    tracker = WindowActivityTracker()
    tracker.observe(WindowInfo("Exam App", "Question 1", available=True))
    result = tracker.observe(WindowInfo("Exam App", "Question 2", available=True))
    assert result == (
        "WINDOW_FOCUS_CHANGED",
        {
            "previous_application": "Exam App",
            "current_application": "Exam App",
            "previous_title": "Question 1",
            "current_title": "Question 2",
        },
    )


def test_app_change_between_non_browsers_is_application_changed():
    from monitors.window_monitor import WindowActivityTracker, WindowInfo

    tracker = WindowActivityTracker()
    tracker.observe(WindowInfo("Exam App", "Exam Window", available=True))
    result = tracker.observe(WindowInfo("Notes", "Untitled", available=True))
    assert result[0] == "APPLICATION_CHANGED"


def test_leaving_a_browser_is_browser_focus_lost():
    from monitors.window_monitor import WindowActivityTracker, WindowInfo

    tracker = WindowActivityTracker()
    tracker.observe(WindowInfo("Google Chrome", "Some Tab", available=True))
    result = tracker.observe(WindowInfo("Notes", "Untitled", available=True))
    assert result[0] == "BROWSER_FOCUS_LOST"


def test_switching_to_a_browser_is_application_changed_not_browser_focus_lost():
    from monitors.window_monitor import WindowActivityTracker, WindowInfo

    tracker = WindowActivityTracker()
    tracker.observe(WindowInfo("Exam App", "Exam Window", available=True))
    result = tracker.observe(WindowInfo("Safari", "New Tab", available=True))
    assert result[0] == "APPLICATION_CHANGED"


def test_unavailable_reports_once_then_suppresses_repeats():
    from monitors.window_monitor import WindowActivityTracker, WindowInfo

    tracker = WindowActivityTracker()
    unavailable = WindowInfo(None, None, available=False, error_message="permission denied")

    first = tracker.observe(unavailable)
    second = tracker.observe(unavailable)

    assert first == ("WINDOW_MONITOR_UNAVAILABLE", {"reason": "permission denied"})
    assert second is None


def test_recovery_after_unavailable_establishes_new_baseline():
    from monitors.window_monitor import WindowActivityTracker, WindowInfo

    tracker = WindowActivityTracker()
    tracker.observe(WindowInfo(None, None, available=False, error_message="denied"))
    baseline = tracker.observe(WindowInfo("Exam App", "Window", available=True))
    assert baseline is None  # first available observation after an outage is just a new baseline


def test_is_browser_helper():
    from monitors.window_monitor import is_browser

    assert is_browser("Google Chrome") is True
    assert is_browser("Safari") is True
    assert is_browser("Notes") is False
    assert is_browser(None) is False
