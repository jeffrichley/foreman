"""ImplementingState — Worker; BLOCKED stays in state pending Poller re-check."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.state import StateContext
from foreman.v4.states.implementing import ImplementingState


def _o(kind: OutcomeKind) -> Outcome:
    return Outcome(kind=kind, confidence=OutcomeConfidence.HIGH, summary="x")


@pytest.mark.parametrize(
    "kind,expected_state_name",
    [
        (OutcomeKind.CLEAN, "ImplReview"),
        (OutcomeKind.BLOCKED, "Implementing"),
        (OutcomeKind.NEEDS_HELP, "NeedsHelp"),
        (OutcomeKind.ERROR, "Failed"),
    ],
)
def test_routing(kind, expected_state_name):
    # foreman#361: per-state routing lives on ``next_state_for``;
    # ``next_state`` is now the Template Method (intercepts
    # TRANSIENT_PROVIDER_ERROR, otherwise delegates). The unit test
    # for routing keeps calling the underlying ``next_state_for``
    # so it doesn't need a real ``StateContext``.
    next_state = ImplementingState().next_state_for(_o(kind))
    assert next_state is not None
    assert next_state.state_name == expected_state_name


def test_blocked_returns_new_implementing_instance():
    """Same logical state, new instance — Poller picks it up next tick."""
    state = ImplementingState()
    next_state = state.next_state_for(_o(OutcomeKind.BLOCKED))
    assert isinstance(next_state, ImplementingState)
    assert next_state is not state


def test_role_attribute():
    assert ImplementingState.role == "worker"


# ---------------------------------------------------------------------
# Issue #342: BLOCKED-retry idempotency end-to-end regression.
#
# Before the fix: when ``ImplementingState`` re-dispatched the Worker
# subprocess after a BLOCKED outcome (foreman#453's exempt-from-retry-
# cap rule), the Worker re-called ``repo.create_pull(...)`` on
# ``foreman/impl-<N>``, GitHub returned 422 ("A pull request already
# exists"), the subprocess crashed, and ``run_worker_cli`` emitted
# ``OutcomeKind.ERROR`` — the state machine then transitioned the
# ticket to ``FailedState``, killing the autonomous loop while CI was
# still in flight on a healthy impl PR.
#
# This regression pin asserts that two consecutive ``ImplementingState``
# dispatches through the ``RoleDispatchState`` seam (where the second
# dispatch sees the same BLOCKED-shaped outcome the first did) BOTH
# emit BLOCKED, never ERROR. The bug fix lives in
# ``foreman.roles.worker._run_worker_core`` and is asserted directly in
# ``test_worker_core.py``; this is the e2e route confirming the
# state-machine consumer is still BLOCKED-routing.
# ---------------------------------------------------------------------


def _impl_blocked_stdout(pr_number: int) -> str:
    """Build a Worker stdout with the BLOCKED-shaped FOREMAN_OUTCOME marker."""
    return (
        'FOREMAN_OUTCOME:{"kind":"blocked","confidence":"high",'
        f'"summary":"impl PR open, check still in flight",'
        f'"artifacts":{{"pr_number":{pr_number}}}}}\n'
    )


def _make_implementing_ctx(
    dispatcher: FakeRoleDispatcher,
) -> tuple[StateContext, InMemoryTicketRepository]:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=342, now=dt.datetime(2026, 6, 18))
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Implementing",
        sequence=1,
        now=dt.datetime(2026, 6, 18),
    )
    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 18),
        role_dispatcher=dispatcher,
    )
    return ctx, repo


def test_implementing_retry_with_blocked_does_not_transition_to_failed():
    """Issue #342, sub-request 10: two consecutive ``ImplementingState``
    dispatches with a BLOCKED-shaped FOREMAN_OUTCOME both produce
    BLOCKED outcomes and route back to ``ImplementingState`` — never
    ERROR / ``FailedState``.

    The Worker fix (existing-PR detection in ``_run_worker_core``)
    keeps the second subprocess from crashing on a duplicate
    ``create_pull`` call; this state-side test confirms the consumer
    keeps re-polling on the BLOCKED signal rather than tripping to
    Failed on the duplicate.
    """
    dispatcher = FakeRoleDispatcher(
        responses={
            ("worker", "p", 342): _impl_blocked_stdout(pr_number=9001),
        }
    )
    ctx, _ = _make_implementing_ctx(dispatcher)

    # First dispatch: BLOCKED → route back to ImplementingState.
    state_1 = ImplementingState()
    outcome_1 = state_1.execute(ctx)
    assert outcome_1.kind == OutcomeKind.BLOCKED, (
        f"first dispatch must produce BLOCKED, got {outcome_1.kind}"
    )
    assert outcome_1.artifacts.pr_number == 9001
    next_state_1 = state_1.next_state(ctx, outcome_1)
    assert isinstance(next_state_1, ImplementingState), (
        f"BLOCKED must route back to ImplementingState, got "
        f"{type(next_state_1).__name__}"
    )

    # Second dispatch on the new ImplementingState instance — the
    # critical re-dispatch path where the foreman#337 bug fired:
    # without the worker-side fix the subprocess would crash with 422
    # and dispatcher stdout would carry ``OutcomeKind.ERROR``.
    outcome_2 = next_state_1.execute(ctx)
    assert outcome_2.kind == OutcomeKind.BLOCKED, (
        f"second dispatch must produce BLOCKED, NOT ERROR. Got "
        f"{outcome_2.kind} — this is the foreman#342 regression."
    )
    assert outcome_2.kind != OutcomeKind.ERROR
    next_state_2 = next_state_1.next_state(ctx, outcome_2)
    assert isinstance(next_state_2, ImplementingState), (
        f"second BLOCKED must also route back to ImplementingState, "
        f"never FailedState. Got {type(next_state_2).__name__}"
    )

    # Defense-in-depth: confirm two distinct worker dispatches actually
    # happened (the bug shape was a SECOND subprocess that crashed; if
    # we only saw one call, the test would be vacuous).
    assert len(dispatcher.calls) == 2
    assert all(call[0] == "worker" for call in dispatcher.calls)
