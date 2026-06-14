"""MergingState — artifact-check against MergeQueue verdict."""
from __future__ import annotations

import datetime as dt

from foreman.v4.git_provider import FakeGitProvider, MergeVerdict, PRState
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


def _ctx_with_pr(pr_number: int = 99) -> tuple[StateContext, InMemoryTicketRepository, FakeGitProvider]:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(ticket.id, "Merging", now=dt.datetime(2026, 6, 13))
    _seed_prior_outcome(repo, ticket.id, pr_number)
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Merging", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=pr_number,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    ctx = StateContext(
        ticket=repo.get_ticket(ticket.id), instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        git=git,
    )
    return ctx, repo, git


def test_first_entry_enqueues_into_merge_queue():
    ctx, repo, git = _ctx_with_pr(pr_number=99)
    MergingState().transition(ctx)
    assert ("p", 99) in git.merge_queue


def test_pending_verdict_routes_back_to_merging():
    ctx, repo, git = _ctx_with_pr(pr_number=99)
    git.enqueue_merge_queue(project="p", pr_number=99)  # already pending
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Merging"


def test_merged_verdict_routes_to_done():
    ctx, repo, git = _ctx_with_pr(pr_number=99)
    git.enqueue_merge_queue(project="p", pr_number=99)
    git.set_merge_verdict(project="p", pr_number=99, verdict=MergeVerdict.MERGED)
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Done"


def test_rejected_verdict_routes_to_impl_fix():
    ctx, repo, git = _ctx_with_pr(pr_number=99)
    git.enqueue_merge_queue(project="p", pr_number=99)
    git.set_merge_verdict(project="p", pr_number=99, verdict=MergeVerdict.REJECTED)
    next_state = MergingState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "ImplFix"


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
    assert closed.failure_phase in ("enter", "execute")
