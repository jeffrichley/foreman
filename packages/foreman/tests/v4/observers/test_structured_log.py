"""StructuredLogObserver — JSON-lines emission per event."""

from __future__ import annotations

import datetime as dt
import json
import logging

import pytest

from foreman.v4.events import (
    ExecuteCompletedEvent,
    StateEnteredEvent,
    StateFailedEvent,
)
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind

_T0 = dt.datetime(2026, 6, 13, 12, 0, 0)


@pytest.fixture()
def observer_and_records(caplog):
    caplog.set_level(logging.INFO, logger="foreman.v4.transitions")
    obs = StructuredLogObserver(logger_name="foreman.v4.transitions")
    return obs, caplog


def test_state_entered_emits_one_json_line(observer_and_records):
    obs, caplog = observer_and_records
    obs(
        StateEnteredEvent(
            ticket_id=1,
            instance_id=10,
            state_name="Planning",
            sequence=1,
            at=_T0,
        )
    )
    record = caplog.records[-1]
    payload = json.loads(record.message)
    assert payload["event"] == "state_entered"
    assert payload["ticket_id"] == 1
    assert payload["state"] == "Planning"
    assert payload["sequence"] == 1


def test_execute_completed_includes_outcome_and_next_state(observer_and_records):
    obs, caplog = observer_and_records
    obs(
        ExecuteCompletedEvent(
            ticket_id=1,
            instance_id=10,
            state_name="Planning",
            sequence=1,
            at=_T0,
            outcome=Outcome(
                kind=OutcomeKind.CLEAN,
                confidence=OutcomeConfidence.HIGH,
                summary="spec PR open",
            ),
            next_state="SpecReview",
        )
    )
    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "execute_completed"
    assert payload["outcome_kind"] == "clean"
    assert payload["confidence"] == "high"
    assert payload["next_state"] == "SpecReview"
    assert payload["summary"] == "spec PR open"


def test_state_failed_uses_warning_level(observer_and_records):
    obs, caplog = observer_and_records
    obs(
        StateFailedEvent(
            ticket_id=1,
            instance_id=10,
            state_name="Planning",
            sequence=1,
            at=_T0,
            failure_phase="execute",
            failure_reason="timeout",
        )
    )
    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    payload = json.loads(record.message)
    assert payload["event"] == "state_failed"
    assert payload["failure_phase"] == "execute"
    assert payload["failure_reason"] == "timeout"
