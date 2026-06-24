"""ImplApprovedState — parked state for an approved impl PR (foreman#418).

Mirrors NeedsHelp exactly: a no-work, non-dispatching, terminal-for-the-
machine state. It means "impl approved, awaiting human merge" and is the
default landing for a CLEAN impl review when the project has not opted in
to ``auto_merge_impl``.
"""
from __future__ import annotations

import datetime as dt

from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import _TERMINAL_STATE_NAMES, StateContext
from foreman.v4.states.impl_approved import ImplApprovedState


def _ctx() -> tuple[StateContext, InMemoryTicketRepository]:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="ImplApproved", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
    )
    return ctx, repo


def test_impl_approved_state_name() -> None:
    assert ImplApprovedState().state_name == "ImplApproved"


def test_impl_approved_does_not_dispatch_a_role() -> None:
    """Parked state — no role attribute, and execute() returns a CLEAN
    landing outcome without touching a role_dispatcher (none provided)."""
    ctx, _repo = _ctx()
    state = ImplApprovedState()
    assert not hasattr(state, "role")
    outcome = state.execute(ctx)
    assert outcome.kind == OutcomeKind.CLEAN


def test_impl_approved_next_state_returns_none() -> None:
    ctx, _repo = _ctx()
    state = ImplApprovedState()
    outcome = state.execute(ctx)
    assert state.next_state(ctx, outcome) is None


def test_impl_approved_is_terminal_for_the_machine() -> None:
    """In ``_TERMINAL_STATE_NAMES`` so transition() synthesizes the
    landing event and the WorkerPool won't re-enqueue."""
    assert "ImplApproved" in _TERMINAL_STATE_NAMES


def test_impl_approved_transition_persists_clean_outcome() -> None:
    ctx, repo = _ctx()
    result = ImplApprovedState().transition(ctx)
    assert result is None
    closed = repo.get_state_instance(ctx.instance.id)
    assert not closed.is_in_flight
    assert closed.outcome_kind == OutcomeKind.CLEAN
