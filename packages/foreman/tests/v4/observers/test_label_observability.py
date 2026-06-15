"""LabelObservabilityObserver — granular add/remove per state transition.

Phase 8c.4 switched the observer from ``write_labels(set)`` (which
replaced the entire label set via PyGithub's ``set_labels`` and
stripped the trigger label) to ``add_labels`` / ``remove_labels``
across StateEntered / StateExited events. These tests pin the
preservation behavior — especially that ``foreman:plan`` is never in
any writer call, which is the bug that wedged the 2026-06-15 dogfood.
"""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.events import (
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
)
from foreman.v4.observers.label_observability import LabelObservabilityObserver
from foreman.v4.repository import InMemoryTicketRepository


class _RecordingWriter:
    """Records every add_labels / remove_labels call for assertion.

    The Protocol expects keyword-only args; we record them in a
    discriminated tuple so a single assertion can cover ordering AND
    op-kind across a multi-transition sequence."""

    def __init__(self) -> None:
        self.add_calls: list[tuple[str, int, set[str]]] = []
        self.remove_calls: list[tuple[str, int, set[str]]] = []
        # Ordered (op, project, issue_number, labels) for sequence
        # assertions — lets test_observer_does_not_touch_trigger_label
        # scan every call across both ops in one pass.
        self.all_calls: list[tuple[str, str, int, set[str]]] = []

    def add_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None:
        self.add_calls.append((project, issue_number, set(labels)))
        self.all_calls.append(("add", project, issue_number, set(labels)))

    def remove_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None:
        self.remove_calls.append((project, issue_number, set(labels)))
        self.all_calls.append(("remove", project, issue_number, set(labels)))


_T0 = dt.datetime(2026, 6, 15)


def _make_repo_and_ticket(state_name: str = "Planning"):
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="foreman", issue_number=42, now=_T0)
    repo.set_ticket_state(ticket.id, state_name, now=_T0)
    return repo, repo.get_ticket(ticket.id)


def test_observer_adds_state_label_on_state_entered() -> None:
    """StateEntered triggers add_labels with the new state's label only."""
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
    assert writer.add_calls == [("foreman", 42, {"foreman:state-planning"})]
    assert writer.remove_calls == []


def test_observer_removes_state_label_on_state_exited() -> None:
    """StateExited triggers remove_labels with the exited state's label."""
    repo, ticket = _make_repo_and_ticket("Planning")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(
        StateExitedEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="Planning",
            sequence=1,
            at=_T0,
            outcome=None,
        )
    )
    assert writer.remove_calls == [("foreman", 42, {"foreman:state-planning"})]
    assert writer.add_calls == []


def test_state_label_lowercases_state_name() -> None:
    """SpecReview → foreman:state-specreview (lowercase). Same shape on
    both add and remove paths."""
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
    obs(
        StateExitedEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="SpecReview",
            sequence=1,
            at=_T0,
            outcome=None,
        )
    )
    assert writer.add_calls[0][2] == {"foreman:state-specreview"}
    assert writer.remove_calls[0][2] == {"foreman:state-specreview"}


def test_ignores_non_lifecycle_events() -> None:
    """Observer only acts on StateEnteredEvent + StateExitedEvent.
    Other events (ExecuteStarted, ExecuteCompleted, StateFailed) are
    silently ignored — they don't change which state-progress label
    should be on the issue."""
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
    assert writer.add_calls == []
    assert writer.remove_calls == []


def test_observer_does_not_touch_trigger_label() -> None:
    """The defining test for Phase 8c.4.

    Simulates a Planning → SpecReview transition sequence
    (StateExited Planning, StateEntered SpecReview). Asserts that
    ``foreman:plan`` appears in NO add_labels and NO remove_labels
    call. This is the trigger-label preservation that the morning's
    dogfood wedged 40min waiting on — if the daemon crashed after
    Phase 8.1's set_labels replaced the entire label set, the Poller
    couldn't re-find the ticket because foreman:plan was gone."""
    repo, ticket = _make_repo_and_ticket("SpecReview")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)

    # Full transition: exit Planning, enter SpecReview.
    obs(
        StateExitedEvent(
            ticket_id=ticket.id,
            instance_id=98,
            state_name="Planning",
            sequence=1,
            at=_T0,
            outcome=None,
        )
    )
    obs(
        StateEnteredEvent(
            ticket_id=ticket.id,
            instance_id=99,
            state_name="SpecReview",
            sequence=2,
            at=_T0,
        )
    )

    # Every single call's label set is inspected — the trigger label
    # must NEVER appear in any of them, on either op.
    for op, _, _, labels in writer.all_calls:
        assert "foreman:plan" not in labels, (
            f"foreman:plan leaked into {op}_labels call: {labels}"
        )

    # Sanity: the observer DID do its job (added new, removed old).
    assert writer.add_calls == [("foreman", 42, {"foreman:state-specreview"})]
    assert writer.remove_calls == [("foreman", 42, {"foreman:state-planning"})]


def test_writer_failure_propagates() -> None:
    """Label-write failure propagates — the EventBus owns the firewall,
    not the observer. We assert the exception class so EventBus's
    blanket except sees it."""

    class _BoomWriter:
        def add_labels(
            self, *, project: str, issue_number: int, labels: set[str]
        ) -> None:
            raise RuntimeError("network down")

        def remove_labels(
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
