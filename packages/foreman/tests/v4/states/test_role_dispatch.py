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
from foreman.v4.session_ids import derive_session_id
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
        ticket_id=ticket.id,
        state_name="Demo",
        sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        role_dispatcher=dispatcher,
    )
    return ctx, repo


def test_dispatches_role_and_parses_outcome():
    dispatcher = FakeRoleDispatcher(
        responses={
            (
                "planner",
                "p",
                1,
            ): 'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}',
        }
    )
    ctx, _ = _make_ctx(dispatcher)
    outcome = _Demo().execute(ctx)
    assert outcome.kind == OutcomeKind.CLEAN
    assert dispatcher.calls == [("planner", "p", 1, ctx.ticket.id)]


def test_missing_marker_propagates_as_outcome_missing():
    dispatcher = FakeRoleDispatcher(
        responses={
            ("planner", "p", 1): "lots of log lines but no marker\n",
        }
    )
    ctx, _ = _make_ctx(dispatcher)
    with pytest.raises(OutcomeMissingError):
        _Demo().execute(ctx)


def test_malformed_json_propagates_as_outcome_malformed():
    dispatcher = FakeRoleDispatcher(
        responses={
            ("planner", "p", 1): "FOREMAN_OUTCOME:{not valid}\n",
        }
    )
    ctx, _ = _make_ctx(dispatcher)
    with pytest.raises(OutcomeMalformedError):
        _Demo().execute(ctx)


def test_missing_dispatcher_raises_at_execute():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Demo",
        sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        # role_dispatcher omitted
    )
    with pytest.raises(RuntimeError) as exc:
        _Demo().execute(ctx)
    assert "role_dispatcher" in str(exc.value).lower()


def test_first_run_stamps_session_id_and_dispatches_fresh():
    """Crash-recovery resume arm: a FIRST run (no prior) stamps the derived
    session id on the instance row and dispatches with ``resume=False``."""
    dispatcher = FakeRoleDispatcher(
        responses={
            (
                "planner",
                "p",
                1,
            ): 'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}',
        }
    )
    ctx, repo = _make_ctx(dispatcher)

    # The id resolve_dispatch will derive for a first run: run_key is the
    # current instance's own sequence (no earlier same-state rows in the run).
    expected_id = derive_session_id(ctx.ticket.id, "planner", None, str(ctx.instance.sequence))

    _Demo().execute(ctx)

    # Persisted on THIS instance's row (non-None and == derived id).
    stored = repo.get_state_instance(ctx.instance.id)
    assert stored.session_id is not None
    assert stored.session_id == expected_id

    # Dispatcher received the same id + resume=False (fresh, no prior).
    assert dispatcher.resume_calls == [(expected_id, False)]


def test_interrupted_prior_with_matching_id_resumes():
    """A prior interrupted same-state attempt whose stored session id matches
    the run's derived id makes execute forward ``resume=True``."""
    dispatcher = FakeRoleDispatcher(
        responses={
            (
                "planner",
                "p",
                1,
            ): 'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}',
        }
    )
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))

    # Prior attempt (seq 1): same state, started but never completed
    # (interrupted), and carrying the id derived for THIS run. run_key is the
    # first sequence in the run (1), so derive with run_key="1".
    prior = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Demo",
        sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    run_id = derive_session_id(ticket.id, "planner", None, "1")
    repo.mark_execute_started(prior.id, now=dt.datetime(2026, 6, 13))
    repo.set_session_id(prior.id, run_id)

    # Current in-flight instance (seq 2): same state → same run as the prior.
    current = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Demo",
        sequence=2,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket,
        instance=current,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        role_dispatcher=dispatcher,
    )

    _Demo().execute(ctx)

    # The derived id is stable across the run (run_key=first seq=1), so the
    # current instance stores the SAME id, and resume is verified True.
    stored = repo.get_state_instance(current.id)
    assert stored.session_id == run_id
    assert dispatcher.resume_calls == [(run_id, True)]
