"""TicketState ABC and StateContext shape."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext, TicketState


class _ConcreteState(TicketState):
    state_name = "Concrete"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary="ok",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return None


def test_ticket_state_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        TicketState()  # type: ignore[abstract]


def test_concrete_state_uses_class_name_default():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Concrete", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    state = _ConcreteState()
    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
    )
    assert state.can_run(ctx) is True  # default True
    assert state.enter(ctx) is None    # default no-op
    outcome = state.execute(ctx)
    assert outcome.kind == OutcomeKind.CLEAN
    state.verify(ctx, outcome)         # default no-op
    state.exit(ctx, outcome)           # default no-op


def test_default_can_run_respects_hold():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.hold_ticket(ticket.id, held_by="jeff", reason="vacation", now=dt.datetime(2026, 6, 13))
    held_ticket = repo.get_ticket(ticket.id)
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Concrete", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    state = _ConcreteState()
    ctx = StateContext(
        ticket=held_ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
    )
    assert state.can_run(ctx) is False
