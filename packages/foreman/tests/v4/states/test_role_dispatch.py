"""RoleDispatchState — common dispatch + outcome-parse mechanism."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.outcome import (
    Outcome,
    OutcomeKind,
    OutcomeMalformedError,
    OutcomeMissingError,
)
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class _Demo(RoleDispatchState):
    state_name = "Demo"
    role = "planner"

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        return None


def _make_ctx(dispatcher: FakeRoleDispatcher) -> tuple[StateContext, InMemoryTicketRepository]:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Demo", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        role_dispatcher=dispatcher,
    )
    return ctx, repo


def test_dispatches_role_and_parses_outcome():
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1):
            'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}',
    })
    ctx, _ = _make_ctx(dispatcher)
    outcome = _Demo().execute(ctx)
    assert outcome.kind == OutcomeKind.CLEAN
    assert dispatcher.calls == [("planner", "p", 1, ctx.ticket.id)]


def test_missing_marker_propagates_as_outcome_missing():
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1): "lots of log lines but no marker\n",
    })
    ctx, _ = _make_ctx(dispatcher)
    with pytest.raises(OutcomeMissingError):
        _Demo().execute(ctx)


def test_malformed_json_propagates_as_outcome_malformed():
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1): "FOREMAN_OUTCOME:{not valid}\n",
    })
    ctx, _ = _make_ctx(dispatcher)
    with pytest.raises(OutcomeMalformedError):
        _Demo().execute(ctx)


def test_missing_dispatcher_raises_at_execute():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Demo", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        # role_dispatcher omitted
    )
    with pytest.raises(RuntimeError) as exc:
        _Demo().execute(ctx)
    assert "role_dispatcher" in str(exc.value).lower()
