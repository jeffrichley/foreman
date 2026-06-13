"""Concrete event types — shape contract for the notification stream."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.events import (
    Event,
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind

_T0 = dt.datetime(2026, 6, 13, 12, 0, 0)


def test_state_entered_event_fields():
    ev = StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    )
    assert ev.ticket_id == 1
    assert ev.state_name == "Planning"
    assert ev.at == _T0


def test_execute_started_event_fields():
    ev = ExecuteStartedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    )
    assert ev.instance_id == 10


def test_execute_completed_event_carries_outcome():
    outcome = Outcome(
        kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
        summary="ok",
    )
    ev = ExecuteCompletedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0, outcome=outcome, next_state="SpecReview",
    )
    assert ev.outcome is outcome
    assert ev.next_state == "SpecReview"


def test_state_exited_event_carries_optional_outcome():
    ev_with = StateExitedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        outcome=Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        ),
    )
    ev_without = StateExitedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0, outcome=None,
    )
    assert ev_with.outcome is not None
    assert ev_without.outcome is None


def test_state_failed_event_carries_phase_and_reason():
    ev = StateFailedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        failure_phase="execute", failure_reason="subprocess timed out",
    )
    assert ev.failure_phase == "execute"
    assert ev.failure_reason == "subprocess timed out"


def test_all_event_classes_are_subclasses_of_event():
    for cls in (
        StateEnteredEvent, ExecuteStartedEvent, ExecuteCompletedEvent,
        StateExitedEvent, StateFailedEvent,
    ):
        assert issubclass(cls, Event)


def test_events_are_immutable():
    ev = StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    )
    with pytest.raises(AttributeError):
        ev.state_name = "Something"  # type: ignore[misc]
