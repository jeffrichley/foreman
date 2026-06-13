"""Tests for the in-memory TicketRepository implementation.

These tests double as the spec for the TicketRepository Protocol — the same
test suite runs against the SqliteTicketRepository in Task 1.7 to guarantee
behavioral parity between the two implementations.
"""
import datetime as dt

import pytest

from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import (
    InMemoryTicketRepository,
    TicketAlreadyExistsError,
    TicketNotFoundError,
)


@pytest.fixture()
def repo() -> InMemoryTicketRepository:
    return InMemoryTicketRepository()


def _now() -> dt.datetime:
    return dt.datetime(2026, 6, 13, 12, 0, 0)


def test_create_ticket_returns_record_with_id(repo: InMemoryTicketRepository):
    ticket = repo.create_ticket(project="foreman", issue_number=42, now=_now())
    assert ticket.id > 0
    assert ticket.project == "foreman"
    assert ticket.issue_number == 42
    assert ticket.current_state == "Queued"
    assert ticket.held_by is None


def test_create_ticket_duplicate_raises(repo: InMemoryTicketRepository):
    repo.create_ticket(project="foreman", issue_number=1, now=_now())
    with pytest.raises(TicketAlreadyExistsError):
        repo.create_ticket(project="foreman", issue_number=1, now=_now())


def test_get_ticket_missing_raises(repo: InMemoryTicketRepository):
    with pytest.raises(TicketNotFoundError):
        repo.get_ticket(999)


def test_get_ticket_by_issue(repo: InMemoryTicketRepository):
    created = repo.create_ticket(project="foreman", issue_number=7, now=_now())
    fetched = repo.get_ticket_by_issue(project="foreman", issue_number=7)
    assert fetched == created


def test_list_open_tickets_excludes_done_and_failed(repo: InMemoryTicketRepository):
    a = repo.create_ticket(project="p", issue_number=1, now=_now())
    b = repo.create_ticket(project="p", issue_number=2, now=_now())
    c = repo.create_ticket(project="p", issue_number=3, now=_now())
    repo.set_ticket_state(a.id, "Done", now=_now())
    repo.set_ticket_state(b.id, "Failed", now=_now())
    open_tickets = repo.list_open_tickets()
    assert {t.issue_number for t in open_tickets} == {c.issue_number}


def test_set_ticket_state_updates(repo: InMemoryTicketRepository):
    t = repo.create_ticket(project="p", issue_number=1, now=_now())
    repo.set_ticket_state(t.id, "Planning", now=dt.datetime(2026, 6, 13, 13))
    fetched = repo.get_ticket(t.id)
    assert fetched.current_state == "Planning"
    assert fetched.updated_at == dt.datetime(2026, 6, 13, 13)


def test_hold_and_resume(repo: InMemoryTicketRepository):
    t = repo.create_ticket(project="p", issue_number=1, now=_now())
    repo.hold_ticket(t.id, held_by="jeff", reason="vacation", now=_now())
    held = repo.get_ticket(t.id)
    assert held.is_held
    assert held.held_by == "jeff"
    assert held.held_reason == "vacation"
    repo.resume_ticket(t.id, now=_now())
    resumed = repo.get_ticket(t.id)
    assert not resumed.is_held
    assert resumed.held_by is None


def test_open_state_instance(repo: InMemoryTicketRepository):
    t = repo.create_ticket(project="p", issue_number=1, now=_now())
    instance = repo.open_state_instance(
        ticket_id=t.id, state_name="Planning", sequence=1, now=_now()
    )
    assert instance.id > 0
    assert instance.is_in_flight
    assert instance.entered_at == _now()


def test_state_instance_lifecycle_timestamps(repo: InMemoryTicketRepository):
    t = repo.create_ticket(project="p", issue_number=1, now=_now())
    inst = repo.open_state_instance(
        ticket_id=t.id, state_name="Planning", sequence=1, now=_now()
    )
    repo.mark_execute_started(inst.id, now=dt.datetime(2026, 6, 13, 12, 1))
    repo.mark_execute_completed(
        inst.id,
        now=dt.datetime(2026, 6, 13, 12, 5),
        outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={"summary": "spec PR open"},
        next_state="SpecReview",
    )
    repo.close_state_instance(inst.id, now=dt.datetime(2026, 6, 13, 12, 6))
    closed = repo.get_state_instance(inst.id)
    assert closed.execute_started_at == dt.datetime(2026, 6, 13, 12, 1)
    assert closed.execute_completed_at == dt.datetime(2026, 6, 13, 12, 5)
    assert closed.exited_at == dt.datetime(2026, 6, 13, 12, 6)
    assert closed.outcome_kind == OutcomeKind.CLEAN
    assert closed.next_state == "SpecReview"
    assert not closed.is_in_flight


def test_list_in_flight_state_instances(repo: InMemoryTicketRepository):
    t = repo.create_ticket(project="p", issue_number=1, now=_now())
    done = repo.open_state_instance(
        ticket_id=t.id, state_name="Queued", sequence=1, now=_now()
    )
    repo.mark_execute_started(done.id, now=_now())
    repo.mark_execute_completed(
        done.id, now=_now(), outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={}, next_state="Planning",
    )
    repo.close_state_instance(done.id, now=_now())
    in_flight = repo.open_state_instance(
        ticket_id=t.id, state_name="Planning", sequence=2, now=_now()
    )
    rows = repo.list_in_flight_state_instances()
    assert [r.id for r in rows] == [in_flight.id]


def test_record_failure_writes_phase_and_reason(repo: InMemoryTicketRepository):
    t = repo.create_ticket(project="p", issue_number=1, now=_now())
    inst = repo.open_state_instance(
        ticket_id=t.id, state_name="Planning", sequence=1, now=_now()
    )
    repo.record_failure(
        inst.id,
        now=_now(),
        failure_phase="execute",
        failure_reason="subprocess timed out",
    )
    fetched = repo.get_state_instance(inst.id)
    assert fetched.failure_phase == "execute"
    assert fetched.failure_reason == "subprocess timed out"
