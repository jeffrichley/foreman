"""MetricsObserver — stub with extensible Protocol."""
from __future__ import annotations

import datetime as dt

from foreman.v4.events import (
    ExecuteCompletedEvent,
    StateEnteredEvent,
    StateFailedEvent,
)
from foreman.v4.observers.metrics import (
    MetricsObserver,
    NoopMetricsBackend,
)
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind

_T0 = dt.datetime(2026, 6, 13)


def test_default_backend_is_noop():
    obs = MetricsObserver()
    # Must not raise on any event:
    obs(StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    ))


def test_custom_backend_receives_increment_and_observation_calls():
    class RecordingBackend:
        def __init__(self) -> None:
            self.increments: list[tuple[str, dict]] = []
            self.observations: list[tuple[str, float, dict]] = []

        def increment(self, name: str, *, tags: dict) -> None:
            self.increments.append((name, tags))

        def observe(self, name: str, value: float, *, tags: dict) -> None:
            self.observations.append((name, value, tags))

    backend = RecordingBackend()
    obs = MetricsObserver(backend=backend)
    obs(StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    ))
    obs(ExecuteCompletedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        outcome=Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        ),
        next_state="SpecReview",
    ))
    obs(StateFailedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        failure_phase="execute", failure_reason="timeout",
    ))
    increment_names = [c[0] for c in backend.increments]
    assert "foreman.v4.state.entered" in increment_names
    assert "foreman.v4.state.completed" in increment_names
    assert "foreman.v4.state.failed" in increment_names


def test_noop_backend_methods_are_callable():
    backend = NoopMetricsBackend()
    backend.increment("x", tags={})
    backend.observe("y", 1.5, tags={})
