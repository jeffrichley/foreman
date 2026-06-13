"""LabelObservabilityObserver — writes one foreman:state-* label per entry."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.events import ExecuteStartedEvent, StateEnteredEvent
from foreman.v4.observers.label_observability import LabelObservabilityObserver
from foreman.v4.repository import InMemoryTicketRepository


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, set[str]]] = []

    def write_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None:
        self.calls.append((project, issue_number, set(labels)))


_T0 = dt.datetime(2026, 6, 13)


def _make_repo_and_ticket(state_name: str = "Planning"):
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="foreman", issue_number=42, now=_T0)
    repo.set_ticket_state(ticket.id, state_name, now=_T0)
    return repo, repo.get_ticket(ticket.id)


def test_state_entered_writes_single_state_label() -> None:
    repo, ticket = _make_repo_and_ticket("Planning")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        StateEnteredEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="Planning",
            sequence=1,
            at=_T0,
        )
    )
    assert writer.calls == [("foreman", 42, {"foreman:state-planning"})]


def test_label_name_lowercases_state() -> None:
    repo, ticket = _make_repo_and_ticket("SpecReview")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        StateEnteredEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="SpecReview",
            sequence=1,
            at=_T0,
        )
    )
    assert writer.calls[0][2] == {"foreman:state-specreview"}


def test_ignores_non_entered_events() -> None:
    """Observer only acts on StateEnteredEvent."""
    repo, ticket = _make_repo_and_ticket()
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        ExecuteStartedEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="Planning",
            sequence=1,
            at=_T0,
        )
    )
    assert writer.calls == []


def test_writer_failure_propagates() -> None:
    """Label-write failure propagates — the EventBus owns the firewall, not
    the observer. We assert the exception class so EventBus's blanket except
    sees it."""

    class _BoomWriter:
        def write_labels(
            self, *, project: str, issue_number: int, labels: set[str]
        ) -> None:
            raise RuntimeError("network down")

    repo, ticket = _make_repo_and_ticket()
    obs = LabelObservabilityObserver(writer=_BoomWriter(), repo=repo)
    with pytest.raises(RuntimeError):
        obs(
            StateEnteredEvent(
                ticket_id=ticket.id,
                instance_id=99,
                state_name="Planning",
                sequence=1,
                at=_T0,
            )
        )
