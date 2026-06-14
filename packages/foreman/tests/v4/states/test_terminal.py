"""Terminal states — Done, Failed, NeedsHelp."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.terminal import DoneState, FailedState, NeedsHelpState


@pytest.fixture()
def ctx_for():
    def _make(state_class):
        repo = InMemoryTicketRepository()
        ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
        instance = repo.open_state_instance(
            ticket_id=ticket.id, state_name=state_class.state_name,
            sequence=1, now=dt.datetime(2026, 6, 13),
        )
        return StateContext(
            ticket=ticket, instance=instance, repo=repo,
            clock=lambda: dt.datetime(2026, 6, 13),
        ), repo, ticket
    return _make


@pytest.mark.parametrize(
    "state_class,expected_name",
    [
        (DoneState, "Done"),
        (FailedState, "Failed"),
        (NeedsHelpState, "NeedsHelp"),
    ],
)
def test_terminal_state_returns_clean_outcome_and_no_next_state(state_class, expected_name, ctx_for):
    ctx, _repo, _ticket = ctx_for(state_class)
    state = state_class()
    assert state.state_name == expected_name
    outcome = state.execute(ctx)
    assert outcome.kind == OutcomeKind.CLEAN
    assert state.next_state(outcome) is None


def test_terminal_transition_persists_outcome(ctx_for):
    ctx, repo, _ticket = ctx_for(DoneState)
    result = DoneState().transition(ctx)
    assert result is None
    closed = repo.get_state_instance(ctx.instance.id)
    assert not closed.is_in_flight
    assert closed.outcome_kind == OutcomeKind.CLEAN
