"""transition() publishes the five lifecycle events at the right boundaries."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.event_bus import EventBus
from foreman.v4.events import (
    Event,
    ExecuteCompletedEvent,
    StateFailedEvent,
)
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext, TicketState


class _ClassicState(TicketState):
    state_name = "Classic"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return None


class _FailEnter(TicketState):
    state_name = "FailEnter"

    def enter(self, ctx: StateContext) -> None:
        raise RuntimeError("enter boom")

    def execute(self, ctx: StateContext) -> Outcome:  # pragma: no cover
        raise NotImplementedError

    def next_state(self, outcome: Outcome) -> TicketState | None:  # pragma: no cover
        return None


@pytest.fixture()
def setup():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Classic", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(received.append)
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
        bus=bus,
    )
    return repo, ticket, instance, received, ctx


def test_happy_path_emits_four_events(setup):
    repo, ticket, instance, received, ctx = setup
    _ClassicState().transition(ctx)
    kinds = [type(ev).__name__ for ev in received]
    assert kinds == [
        "StateEnteredEvent",
        "ExecuteStartedEvent",
        "ExecuteCompletedEvent",
        "StateExitedEvent",
    ]


def test_execute_completed_carries_outcome_and_next_state(setup):
    repo, ticket, instance, received, ctx = setup
    _ClassicState().transition(ctx)
    completed = [ev for ev in received if isinstance(ev, ExecuteCompletedEvent)][0]
    assert completed.outcome.kind == OutcomeKind.CLEAN
    assert completed.next_state == ""  # terminal — no next state


def test_enter_failure_emits_failed_event_no_exit(setup):
    repo, ticket, instance, received, ctx = setup
    _FailEnter().transition(ctx)
    kinds = [type(ev).__name__ for ev in received]
    # enter() raised → no StateEntered, no Execute*, no Exited.
    assert kinds == ["StateFailedEvent"]
    failed = received[0]
    assert isinstance(failed, StateFailedEvent)
    assert failed.failure_phase == "enter"
    assert "enter boom" in failed.failure_reason


def test_no_bus_means_no_events(setup):
    repo, ticket, instance, received, _ctx = setup
    # Rebuild ctx without a bus
    ctx_no_bus = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        bus=None,
    )
    _ClassicState().transition(ctx_no_bus)
    assert received == []  # the original bus saw nothing


def test_misbehaving_observer_does_not_break_transition(setup):
    repo, ticket, instance, _received, ctx = setup

    def boom(_):
        raise RuntimeError("observer boom")

    ctx.bus.subscribe(boom)
    result = _ClassicState().transition(ctx)
    assert result is None  # terminal completion path
    # And the journal row was still finalized:
    closed = repo.get_state_instance(instance.id)
    assert not closed.is_in_flight
