"""EventArchiveObserver — append-only events table for forensics + replay."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from foreman.v4.events import (
    ExecuteCompletedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.sqlite_repository import SqliteTicketRepository

_T0 = dt.datetime(2026, 6, 13, 12, 0, 0)


@pytest.fixture()
def repo_and_ticket():
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=1, now=_T0)
    return repo, ticket


def test_state_entered_writes_one_event_row(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    obs(StateEnteredEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
    ))
    rows = repo._conn.execute("SELECT * FROM events").fetchall()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "state_entered"
    assert rows[0]["state_name"] == "Planning"


def test_execute_completed_payload_carries_outcome(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    obs(ExecuteCompletedEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
        outcome=Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        ),
        next_state="SpecReview",
    ))
    row = repo._conn.execute("SELECT * FROM events").fetchone()
    payload = json.loads(row["payload"])
    assert payload["outcome_kind"] == "clean"
    assert payload["next_state"] == "SpecReview"


def test_state_failed_payload_carries_phase_and_reason(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    obs(StateFailedEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
        failure_phase="execute", failure_reason="timeout",
    ))
    row = repo._conn.execute("SELECT * FROM events").fetchone()
    payload = json.loads(row["payload"])
    assert payload["failure_phase"] == "execute"
    assert payload["failure_reason"] == "timeout"


def test_events_are_append_only(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    for i in range(3):
        obs(StateEnteredEvent(
            ticket_id=ticket.id, instance_id=99,
            state_name="S", sequence=i + 1,
            at=_T0 + dt.timedelta(seconds=i),
        ))
    rows = repo._conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    assert [r["sequence"] for r in rows] == [1, 2, 3]


def test_state_exited_payload_carries_outcome_kind(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    obs(StateExitedEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
        outcome=Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        ),
    ))
    row = repo._conn.execute("SELECT * FROM events").fetchone()
    payload = json.loads(row["payload"])
    assert payload["outcome_kind"] == "clean"


def test_state_exited_payload_none_when_no_outcome(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    obs(StateExitedEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
        outcome=None,
    ))
    row = repo._conn.execute("SELECT * FROM events").fetchone()
    payload = json.loads(row["payload"])
    assert payload["outcome_kind"] is None
