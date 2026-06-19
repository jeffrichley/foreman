"""MergingState — direct pr.merge() against the impl PR.

Phase 8d.19 collapsed the MergeQueue enqueue-then-poll path into one
direct merge call. These tests pin down the 3-branch shape of
``MergingState.execute``:

  - PR already merged externally → CLEAN (no merge_pr call).
  - PR mergeable + CI passing → call merge_pr → CLEAN.
  - Anything else → BLOCKED (Poller picks it up next tick).

Granular ``mergeable_state`` handling (CI failed → ImplFix, dirty →
ImplFix, etc.) is deferred to foreman#317. The BLOCKED branch is the
catch-all today.
"""
from __future__ import annotations

import datetime as dt

from foreman.v4.git_provider import FakeGitProvider, PRState
from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.merging import MergingState


def _seed_prior_outcome(
    repo: InMemoryTicketRepository, ticket_id: int, pr_number: int
) -> None:
    """Seed a prior ExecuteCompleted state instance carrying the PR number.

    Mirrors what ImplReviewState would have written before the ticket reached
    Merging. The repository's `latest_pr_number_for_ticket` then resolves to
    `pr_number`.
    """
    prior = repo.open_state_instance(
        ticket_id=ticket_id, state_name="ImplReview", sequence=0,
        now=dt.datetime(2026, 6, 13),
    )
    repo.mark_execute_completed(
        prior.id, now=dt.datetime(2026, 6, 13),
        outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={"artifacts": {"pr_number": pr_number}},
        next_state="Merging",
    )
    repo.close_state_instance(prior.id, now=dt.datetime(2026, 6, 13))


def _ctx_with_pr(
    pr_number: int = 99, *, pr_state: PRState | None = None,
) -> tuple[StateContext, InMemoryTicketRepository, FakeGitProvider]:
    """Build a StateContext where the ticket is in Merging with the
    named PR seeded against the FakeGitProvider in the given state.

    The default ``pr_state`` is mergeable + CI passing + not yet merged
    — the "execute() should call merge_pr" happy path. Tests override
    when they want to exercise the merged-externally or BLOCKED branches.
    """
    if pr_state is None:
        pr_state = PRState(merged=False, mergeable=True, ci_passing=True)
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(ticket.id, "Merging", now=dt.datetime(2026, 6, 13))
    _seed_prior_outcome(repo, ticket.id, pr_number)
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Merging", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    git = FakeGitProvider()
    git.set_pr_state(project="p", pr_number=pr_number, state=pr_state)
    ctx = StateContext(
        ticket=repo.get_ticket(ticket.id), instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        git=git,
    )
    return ctx, repo, git


def test_merging_state_returns_clean_when_pr_merged_externally():
    """If the PR is already merged by something outside the daemon
    (operator click-merge, GitHub's own merge-queue, an earlier daemon
    instance), execute() returns CLEAN without calling merge_pr again.

    The recorder asymmetry is load-bearing: a naive implementation that
    always calls merge_pr would set merged=True via the fake's state
    machine and "pass" a merged-state assertion, masking the bug class.

    Phase 8d.20 extension: the originating issue must STILL get closed
    on the already-merged branch — otherwise an operator click-merge
    leaves the issue OPEN forever despite the loop reaching Done.
    """
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(merged=True, mergeable=True, ci_passing=True),
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Done"
    # NO merge_pr call — the PR was already merged.
    assert ("p", 99) not in git.merge_pr_calls
    # But the issue MUST still be closed (Phase 8d.20).
    assert ("p", 1) in git.closed_issues


def test_merging_state_calls_merge_pr_when_mergeable_and_ci_passing():
    """The happy path: GitHub reports the PR mergeable + CI green,
    execute() calls merge_pr and returns CLEAN → Done.

    Phase 8d.20 extension: the originating issue is also closed after
    the merge — same call-site, gated on the same CLEAN outcome.
    """
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Done"
    assert ("p", 99) in git.merge_pr_calls
    assert git.get_pr_state(project="p", pr_number=99).merged is True
    # Phase 8d.20: the originating issue is closed after the merge.
    assert ("p", 1) in git.closed_issues


def test_merging_state_returns_blocked_when_ci_pending():
    """CI hasn't passed yet — execute() returns BLOCKED so the Poller
    tries again next tick. merge_pr MUST NOT be called: merging while
    CI is still running would defeat the whole point of the gate."""
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(merged=False, mergeable=True, ci_passing=False),
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Merging"
    assert ("p", 99) not in git.merge_pr_calls
    # And the PR is still un-merged from the daemon's perspective.
    assert git.get_pr_state(project="p", pr_number=99).merged is False


def test_merging_state_returns_blocked_when_not_mergeable():
    """The PR isn't mergeable (conflict, blocked-by-review, etc.) —
    execute() returns BLOCKED. Granular dispatch on the underlying
    cause (rebase needed, review missing, etc.) is foreman#317; this
    task ships the minimum-shape that lets the happy path reach Done."""
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(merged=False, mergeable=False, ci_passing=True),
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Merging"
    assert ("p", 99) not in git.merge_pr_calls


def test_merging_state_blocked_does_not_close_issue():
    """BLOCKED branch (CI pending OR not mergeable) MUST NOT close the
    issue. Closing on every poll tick would prematurely close issues
    whose impl PR isn't actually merged yet — directly user-visible.
    """
    ctx, _repo, git = _ctx_with_pr(
        pr_number=99,
        pr_state=PRState(merged=False, mergeable=False, ci_passing=False),
    )
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Merging"
    assert ("p", 99) not in git.merge_pr_calls
    # Crucial: BLOCKED leaves the issue OPEN.
    assert git.closed_issues == set()


def test_missing_git_provider_routes_through_execute_failure():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    _seed_prior_outcome(repo, ticket.id, 99)
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Merging", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        # git omitted
    )
    MergingState().transition(ctx)
    closed = repo.get_state_instance(instance.id)
    assert closed.failure_phase == "execute"
