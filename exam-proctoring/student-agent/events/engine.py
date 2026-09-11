"""Centralized event engine: turns raw monitor Signals into rate-limited, severity-scored Events.

Responsibilities (spec section 7):
  - debouncing / cooldown periods: a signal of the same event_type occurring
    again too soon after the last emitted event of that type is suppressed.
  - aggregation: monitors track condition duration/repeat counts themselves
    (e.g. "camera failed for 100 consecutive frames") and pass a single
    signal carrying that count; the engine turns it into exactly one event.
  - severity calculation: default severity per event type, escalated to RED
    when a condition has been repeatedly re-signaled (persistence).
  - duplicate suppression: identical (event_type, metadata) signals within
    the cooldown window never produce more than one event.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Callable, Optional

from config import AgentConfig
from events.schemas import DEFAULT_SEVERITY, EventPayload, EventType, Severity, Signal

logger = logging.getLogger("agent.event_engine")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class EventEngine:
    def __init__(self, session_id: str, student_id: str, config: AgentConfig, on_emit: Callable[[EventPayload], None]):
        self.session_id = session_id
        self.student_id = student_id
        self.config = config
        self.on_emit = on_emit

        self._last_emitted_at: dict[str, dt.datetime] = {}
        self._repeat_streak: dict[str, int] = {}  # consecutive same-type emissions -> used to escalate severity
        self._total_events_emitted = 0

    @property
    def total_events_emitted(self) -> int:
        return self._total_events_emitted

    def process_signal(self, signal: Signal) -> Optional[EventPayload]:
        if not self.should_emit(signal):
            return None
        event = self.create_event(signal)
        self._last_emitted_at[signal.event_type.value] = _now()
        self._total_events_emitted += 1
        logger.info(
            "event emitted type=%s severity=%s event_id=%s",
            event.event_type.value,
            event.severity.value,
            event.event_id,
        )
        self.on_emit(event)
        return event

    def should_emit(self, signal: Signal) -> bool:
        cooldown = self.config.cooldown_seconds.get(signal.event_type.value, self.config.default_cooldown_seconds)
        last = self._last_emitted_at.get(signal.event_type.value)
        if last is not None and (_now() - last).total_seconds() < cooldown:
            return False
        return True

    def create_event(self, signal: Signal) -> EventPayload:
        severity = self._calculate_severity(signal)
        return EventPayload(
            student_id=self.student_id,
            session_id=self.session_id,
            timestamp=signal.detected_at,
            event_type=signal.event_type,
            severity=severity,
            metadata=signal.metadata,
        )

    def _calculate_severity(self, signal: Signal) -> Severity:
        base = signal.severity_hint or DEFAULT_SEVERITY.get(signal.event_type, Severity.YELLOW)

        # Streaks are already isolated per event_type (this dict is keyed by
        # it), so no cross-type reset is needed here - and doing one anyway
        # would let any unrelated signal (e.g. a single window-focus change
        # interleaved between camera polls) zero out a persistent condition's
        # progress before it ever reaches the 3-strikes threshold below.
        key = signal.event_type.value
        streak = self._repeat_streak.get(key, 0) + 1
        self._repeat_streak[key] = streak

        # Escalate yellow -> red if the same condition has re-triggered 3+ times
        # in a row (i.e. it is persistent, not a one-off blip).
        if base == Severity.YELLOW and streak >= 3:
            return Severity.RED
        return base

    def reset(self) -> None:
        self._last_emitted_at.clear()
        self._repeat_streak.clear()
