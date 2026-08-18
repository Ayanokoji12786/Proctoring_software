"""EventEngine: creation, debounce/cooldown suppression, severity escalation."""
from __future__ import annotations

import time


def _make_engine(agent_config):
    from events.engine import EventEngine

    emitted = []
    engine = EventEngine(agent_config.session_id, agent_config.student_id, agent_config, on_emit=emitted.append)
    return engine, emitted


def test_event_creation_fields(agent_config):
    from events.schemas import EventType, Severity, Signal

    engine, emitted = _make_engine(agent_config)
    signal = Signal(source="window", event_type=EventType.WINDOW_FOCUS_CHANGED, metadata={"a": 1})
    event = engine.process_signal(signal)

    assert event is not None
    assert emitted == [event]
    assert event.student_id == agent_config.student_id
    assert event.session_id == agent_config.session_id
    assert event.event_type == EventType.WINDOW_FOCUS_CHANGED
    assert event.severity == Severity.YELLOW  # default for this type
    assert event.metadata == {"a": 1}
    assert event.event_id  # non-empty uuid


def test_duplicate_signal_suppressed_within_cooldown(agent_config):
    from events.schemas import EventType, Signal

    agent_config.cooldown_seconds["NO_FACE_DETECTED"] = 5.0
    engine, emitted = _make_engine(agent_config)
    signal = Signal(source="camera", event_type=EventType.NO_FACE_DETECTED, metadata={})

    first = engine.process_signal(signal)
    second = engine.process_signal(signal)

    assert first is not None
    assert second is None
    assert len(emitted) == 1
    assert engine.total_events_emitted == 1


def test_signal_emitted_again_after_cooldown_elapses(agent_config):
    from events.schemas import EventType, Signal

    agent_config.cooldown_seconds["NO_FACE_DETECTED"] = 0.05
    engine, emitted = _make_engine(agent_config)
    signal = Signal(source="camera", event_type=EventType.NO_FACE_DETECTED, metadata={})

    engine.process_signal(signal)
    time.sleep(0.08)
    second = engine.process_signal(signal)

    assert second is not None
    assert len(emitted) == 2


def test_severity_escalates_to_red_after_persistent_repeats(agent_config):
    from events.schemas import EventType, Severity, Signal

    agent_config.cooldown_seconds["NO_FACE_DETECTED"] = 0.02
    engine, emitted = _make_engine(agent_config)
    signal = Signal(source="camera", event_type=EventType.NO_FACE_DETECTED, metadata={})

    severities = []
    for _ in range(4):
        event = engine.process_signal(signal)
        severities.append(event.severity)
        time.sleep(0.03)

    assert severities[0] == Severity.YELLOW
    assert severities[1] == Severity.YELLOW
    assert severities[2] == Severity.RED  # 3rd consecutive occurrence escalates
    assert severities[3] == Severity.RED


def test_unrelated_event_types_do_not_share_escalation_streak(agent_config):
    from events.schemas import EventType, Severity, Signal

    agent_config.cooldown_seconds["NO_FACE_DETECTED"] = 0.02
    agent_config.cooldown_seconds["FACE_AWAY"] = 0.02
    engine, emitted = _make_engine(agent_config)

    no_face = Signal(source="camera", event_type=EventType.NO_FACE_DETECTED, metadata={})
    face_away = Signal(source="camera", event_type=EventType.FACE_AWAY, metadata={})

    engine.process_signal(no_face)
    time.sleep(0.03)
    engine.process_signal(no_face)
    time.sleep(0.03)
    # switching event type resets the other type's streak
    event = engine.process_signal(face_away)

    assert event.severity == Severity.YELLOW  # first occurrence of this type, not escalated


def test_green_severity_never_auto_escalates(agent_config):
    from events.schemas import EventType, Severity, Signal

    agent_config.cooldown_seconds["FACE_PRESENT"] = 0.02
    engine, emitted = _make_engine(agent_config)
    signal = Signal(source="camera", event_type=EventType.FACE_PRESENT, metadata={})

    for _ in range(4):
        event = engine.process_signal(signal)
        time.sleep(0.03)

    assert event.severity == Severity.GREEN
