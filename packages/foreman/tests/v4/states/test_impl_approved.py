"""ImplApprovedState — polling-wait-then-finalize tests (foreman#443).

Covers all four behavior branches:
  merged       → CLEAN + close_originating_issue + DoneState
  open         → BLOCKED + self-loop (ImplApprovedState), no close
  closed-unmerged → NEEDS_HELP + NeedsHelpState, no close
  already-closed issue → CLEAN (idempotent close)

Plus the runaway-cap exemption: N consecutive BLOCKED polls must NOT
trip the cap and escalate to NeedsHelp (BLOCKED rows are skip-listed
by count_consecutive_same_state per Phase 8d.18).
"""
from __future__ import annotations

import datetime as dt

from foreman.v4.git_provider import FakeGitProvider, PRState
from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import _TERMINAL_STATE_NAMES, StateContext
from foreman.v4.states.impl_approved import ImplApprovedState

# ---------------------------------------------------------------------------
# Shared test fixture
# ---------------------------------------------------------------------------

def _ctx(
    pr_state: PRState,
    *,
    repo: InMemoryTicketRepository | None = None,
) -> tuple[StateContext, InMemoryTicketRepository, FakeGitProvider]:
    """Build a StateContext with ImplApproved as the current state.

    Seeds a prior ImplReview row so that ``latest_pr_number_for_ticket``
    returns 42, mimicking the real wiring where ImplReview records
    ``artifacts.pr_number`` in its outcome payload.
    """
    repo = repo or InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))

    # Simulate a prior ImplReview outcome carrying pr_number=42.
    prior = repo.open_state_instance(
        ticket_id=ticket.id, state_name="ImplReview", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    repo.mark_execute_completed(
        prior.id, now=dt.datetime(2026, 6, 13),
        outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={
            "kind": "clean", "confidence": "high", "summary": "test",
            "artifacts": {"pr_number": 42},
        },
        next_state="ImplApproved",
    )
    repo.close_state_instance(prior.id, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(ticket.id, "ImplApproved", now=dt.datetime(2026, 6, 13))
    # Re-fetch the updated ticket record.
    ticket = repo.get_ticket(ticket.id)

    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="ImplApproved", sequence=2,
        now=dt.datetime(2026, 6, 13),
    )
    git = FakeGitProvider()
    git.set_pr_state(project="p", pr_number=42, state=pr_state)

    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        git=git,
    )
    return ctx, repo, git


# ---------------------------------------------------------------------------
# Identity / structural
# ---------------------------------------------------------------------------

def test_impl_approved_state_name() -> None:
    assert ImplApprovedState().state_name == "ImplApproved"


def test_impl_approved_does_not_dispatch_a_role() -> None:
    """ImplApproved only calls git, never the role_dispatcher."""
    assert not hasattr(ImplApprovedState(), "role")


def test_impl_approved_is_NOT_terminal_for_the_machine() -> None:
    """foreman#443: removed from _TERMINAL_STATE_NAMES so the Poller
    re-enqueues it on each tick and the WorkerPool re-dispatches."""
    assert "ImplApproved" not in _TERMINAL_STATE_NAMES


# ---------------------------------------------------------------------------
# Branch: impl PR merged → CLEAN + close + Done
# ---------------------------------------------------------------------------

def test_impl_approved_merged_returns_clean() -> None:
    """When the impl PR is merged, execute() returns CLEAN."""
    pr = PRState(merged=True, closed=True, mergeable=False, ci_passing=False)
    ctx, _repo, _git = _ctx(pr)
    outcome = ImplApprovedState().execute(ctx)
    assert outcome.kind == OutcomeKind.CLEAN


def test_impl_approved_merged_closes_originating_issue() -> None:
    """When merged, the originating GitHub issue is closed."""
    pr = PRState(merged=True, closed=True, mergeable=False, ci_passing=False)
    ctx, _repo, git = _ctx(pr)
    ImplApprovedState().execute(ctx)
    assert ("p", 1) in git.closed_issues


def test_impl_approved_merged_next_state_is_done() -> None:
    """When merged, next_state returns DoneState."""
    from foreman.v4.states.terminal import DoneState
    pr = PRState(merged=True, closed=True, mergeable=False, ci_passing=False)
    ctx, _repo, _git = _ctx(pr)
    state = ImplApprovedState()
    outcome = state.execute(ctx)
    next_ = state.next_state(ctx, outcome)
    assert isinstance(next_, DoneState)


def test_impl_approved_merged_records_pr_artifact() -> None:
    """The CLEAN outcome carries the PR number in its artifacts."""
    pr = PRState(merged=True, closed=True, mergeable=False, ci_passing=False)
    ctx, _repo, _git = _ctx(pr)
    outcome = ImplApprovedState().execute(ctx)
    assert outcome.artifacts is not None
    assert outcome.artifacts.pr_number == 42


# ---------------------------------------------------------------------------
# Branch: impl PR open → BLOCKED + self-loop, no close
# ---------------------------------------------------------------------------

def test_impl_approved_open_returns_blocked() -> None:
    """While the impl PR is open, execute() returns BLOCKED."""
    pr = PRState(merged=False, closed=False, mergeable=False, ci_passing=False)
    ctx, _repo, _git = _ctx(pr)
    outcome = ImplApprovedState().execute(ctx)
    assert outcome.kind == OutcomeKind.BLOCKED


def test_impl_approved_open_does_not_close_issue() -> None:
    """BLOCKED does NOT close the issue (would be premature)."""
    pr = PRState(merged=False, closed=False, mergeable=False, ci_passing=False)
    ctx, _repo, git = _ctx(pr)
    ImplApprovedState().execute(ctx)
    assert git.closed_issues == set()


def test_impl_approved_open_next_state_is_self() -> None:
    """BLOCKED self-loops back to ImplApprovedState."""
    pr = PRState(merged=False, closed=False, mergeable=False, ci_passing=False)
    ctx, _repo, _git = _ctx(pr)
    state = ImplApprovedState()
    outcome = state.execute(ctx)
    next_ = state.next_state(ctx, outcome)
    assert isinstance(next_, ImplApprovedState)


# ---------------------------------------------------------------------------
# Branch: impl PR closed-without-merge → NEEDS_HELP
# ---------------------------------------------------------------------------

def test_impl_approved_closed_without_merge_returns_needs_help() -> None:
    """A closed-but-not-merged impl PR escalates to NeedsHelp."""
    pr = PRState(merged=False, closed=True, mergeable=False, ci_passing=False)
    ctx, _repo, _git = _ctx(pr)
    outcome = ImplApprovedState().execute(ctx)
    assert outcome.kind == OutcomeKind.NEEDS_HELP


def test_impl_approved_closed_without_merge_does_not_close_issue() -> None:
    """NeedsHelp does NOT close the issue (merge never happened)."""
    pr = PRState(merged=False, closed=True, mergeable=False, ci_passing=False)
    ctx, _repo, git = _ctx(pr)
    ImplApprovedState().execute(ctx)
    assert git.closed_issues == set()


def test_impl_approved_closed_without_merge_next_state_is_needs_help() -> None:
    """Closed-without-merge routes to NeedsHelpState."""
    from foreman.v4.states.terminal import NeedsHelpState
    pr = PRState(merged=False, closed=True, mergeable=False, ci_passing=False)
    ctx, _repo, _git = _ctx(pr)
    state = ImplApprovedState()
    outcome = state.execute(ctx)
    next_ = state.next_state(ctx, outcome)
    assert isinstance(next_, NeedsHelpState)


# ---------------------------------------------------------------------------
# Branch: already-closed issue → idempotent close
# ---------------------------------------------------------------------------

def test_impl_approved_already_closed_issue_idempotent() -> None:
    """close_originating_issue is idempotent: calling on an already-closed
    issue does NOT raise and still results in a CLEAN outcome."""
    pr = PRState(merged=True, closed=True, mergeable=False, ci_passing=False)
    ctx, _repo, git = _ctx(pr)

    # Operator (or prior run) already closed the issue.
    git.close_issue(project="p", issue_number=1)
    assert ("p", 1) in git.closed_issues

    outcome = ImplApprovedState().execute(ctx)
    # Still CLEAN — no raise.
    assert outcome.kind == OutcomeKind.CLEAN
    # Still in closed_issues (idempotent add is a no-op).
    assert ("p", 1) in git.closed_issues


# ---------------------------------------------------------------------------
# merged-checked-first: merged=True, closed=True → CLEAN (not NEEDS_HELP)
# ---------------------------------------------------------------------------

def test_impl_approved_merged_wins_over_closed() -> None:
    """GitHub sets both merged=True AND closed=True for a merged PR.
    ImplApproved must check merged FIRST so a merged PR is treated
    as the success branch, not the closed-without-merge branch."""
    pr = PRState(merged=True, closed=True, mergeable=False, ci_passing=False)
    ctx, _repo, _git = _ctx(pr)
    outcome = ImplApprovedState().execute(ctx)
    assert outcome.kind == OutcomeKind.CLEAN


# ---------------------------------------------------------------------------
# Runaway-cap exemption: N BLOCKED polls must NOT escalate to NeedsHelp
# ---------------------------------------------------------------------------

def test_impl_approved_blocked_does_not_count_toward_runaway_cap() -> None:
    """Five consecutive BLOCKED polls (> max_state_attempts=3) must NOT
    escalate to NeedsHelp. BLOCKED rows are excluded from
    count_consecutive_same_state (Phase 8d.18), so the counter stays
    below the cap indefinitely while the human is considering the merge."""
    repo = InMemoryTicketRepository()
    pr_open = PRState(merged=False, closed=False, mergeable=False, ci_passing=False)
    ctx0, _repo, git = _ctx(pr_open, repo=repo)
    ticket_id = ctx0.ticket.id
    # Close the initial _ctx instance (seq=2) before the loop so the new
    # one-open-per-ticket invariant is satisfied when the loop opens each
    # polling instance. The _ctx helper leaves it open but this test
    # exercises the runaway-cap logic, not the initial dispatch path.
    repo.close_state_instance(ctx0.instance.id, now=dt.datetime(2026, 6, 13))

    # Run 5 transition() calls — more than max_state_attempts=3.
    for poll_n in range(1, 6):
        ticket = repo.get_ticket(ticket_id)
        instance = repo.open_state_instance(
            ticket_id=ticket_id,
            state_name="ImplApproved",
            # sequence continues after the prior ImplReview row (seq=1)
            # and the ImplApproved poll rows (starting at seq=2).
            sequence=poll_n + 1,
            now=dt.datetime(2026, 6, 13),
        )
        ctx = StateContext(
            ticket=ticket, instance=instance, repo=repo,
            clock=lambda: dt.datetime(2026, 6, 13),
            git=git,
            max_state_attempts=3,
        )
        result = ImplApprovedState().transition(ctx)
        assert result is not None, f"poll {poll_n}: expected self-loop, got None"
        assert result.state_name == "ImplApproved", (
            f"poll {poll_n}: expected ImplApproved, got {result.state_name!r} "
            "(runaway cap may have tripped)"
        )
