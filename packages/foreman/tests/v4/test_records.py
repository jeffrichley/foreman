"""Read-shape records returned by TicketRepository."""
import datetime as dt

import pytest

from foreman.v4.outcome import OutcomeKind
from foreman.v4.records import StateInstanceRecord, TicketRecord


def test_ticket_record_is_frozen():
    record = TicketRecord(
        id=1,
        project="foreman",
        issue_number=42,
        current_state="Planning",
        created_at=dt.datetime(2026, 6, 13),
        updated_at=dt.datetime(2026, 6, 13),
        held_by=None,
        held_at=None,
        held_reason=None,
    )
    with pytest.raises(AttributeError):
        record.current_state = "Done"  # type: ignore[misc]


def test_ticket_record_is_held_predicate():
    not_held = TicketRecord(
        id=1, project="p", issue_number=1, current_state="Queued",
        created_at=dt.datetime(2026, 6, 13), updated_at=dt.datetime(2026, 6, 13),
        held_by=None, held_at=None, held_reason=None,
    )
    held = TicketRecord(
        id=2, project="p", issue_number=2, current_state="Queued",
        created_at=dt.datetime(2026, 6, 13), updated_at=dt.datetime(2026, 6, 13),
        held_by="jeff", held_at=dt.datetime(2026, 6, 13), held_reason="vacation",
    )
    assert not_held.is_held is False
    assert held.is_held is True


def test_state_instance_record_in_flight_predicate():
    in_flight = StateInstanceRecord(
        id=1, ticket_id=1, state_name="Planning", sequence=1,
        entered_at=dt.datetime(2026, 6, 13),
        execute_started_at=None, execute_completed_at=None,
        exited_at=None, outcome_kind=None, outcome_payload=None,
        next_state=None, failure_phase=None, failure_reason=None,
    )
    done = StateInstanceRecord(
        id=2, ticket_id=1, state_name="Planning", sequence=1,
        entered_at=dt.datetime(2026, 6, 13),
        execute_started_at=dt.datetime(2026, 6, 13),
        execute_completed_at=dt.datetime(2026, 6, 13),
        exited_at=dt.datetime(2026, 6, 13),
        outcome_kind=OutcomeKind.CLEAN, outcome_payload={"summary": "ok"},
        next_state="SpecReview", failure_phase=None, failure_reason=None,
    )
    assert in_flight.is_in_flight is True
    assert done.is_in_flight is False
