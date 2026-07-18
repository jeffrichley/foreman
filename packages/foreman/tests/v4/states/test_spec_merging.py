"""SpecMerging — hands the approved spec PR to the merge queue.

foreman#550 moved the actual merge out of this state (see MergingState's
module docstring for the parallel rationale). SpecMerging's execute() now
only enqueues the spec PR (idempotently, via ``merge_helper.enqueue_for_merge``)
and routes to ``MergeQueued`` — no base-ref guard (spec PRs aren't
constrained by ``dev_base_branch``, unlike impl PRs), no issue-close (the
originating issue closes when the IMPL PR merges, not the spec PR — that
will happen in the coordinator, a later foreman#550 task), and no
GitProvider dependency at all (the PR number comes from the ticket's own
outcome history, not from GitHub).
"""

from __future__ import annotations

import datetime as dt

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.spec_merging import SpecMerging


def _seed_prior_outcome(repo: InMemoryTicketRepository, ticket_id: int, pr_number: int) -> None:
    """Seed the prior SpecReview CLEAN outcome carrying the spec PR number,
    mirroring what SpecReviewState writes before routing to SpecMerging."""
    prior = repo.open_state_instance(
        ticket_id=ticket_id,
        state_name="SpecReview",
        sequence=0,
        now=dt.datetime(2026, 6, 13),
    )
    repo.mark_execute_completed(
        prior.id,
        now=dt.datetime(2026, 6, 13),
        outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={"artifacts": {"pr_number": pr_number}},
        next_state="SpecMerging",
    )
    repo.close_state_instance(prior.id, now=dt.datetime(2026, 6, 13))


def _ctx_with_pr(pr_number: int = 42) -> tuple[StateContext, InMemoryTicketRepository]:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(
        project="p",
        issue_number=1,
        now=dt.datetime(2026, 6, 13),
    )
    repo.set_ticket_state(ticket.id, "SpecMerging", now=dt.datetime(2026, 6, 13))
    _seed_prior_outcome(repo, ticket.id, pr_number)
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="SpecMerging",
        sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=repo.get_ticket(ticket.id),
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        # No git — SpecMerging's hand-off doesn't need a GitProvider.
    )
    return ctx, repo


def test_spec_merging_state_name():
    assert SpecMerging.state_name == "SpecMerging"


def test_spec_merging_enqueues_spec_pr_and_routes_to_merge_queued():
    ctx, repo = _ctx_with_pr(pr_number=42)
    next_state = SpecMerging().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "MergeQueued"

    entries = repo.merge_queue_for_project("p")
    assert len(entries) == 1
    assert entries[0].ticket_id == ctx.ticket.id
    assert entries[0].pr_number == 42
    assert entries[0].kind == "spec"


def test_spec_merging_second_execute_does_not_double_enqueue():
    """A re-dispatched SpecMerging execute() on an already-queued ticket
    must not insert a second merge_queue row for the same ticket."""
    ctx, repo = _ctx_with_pr(pr_number=42)
    SpecMerging().execute(ctx)
    SpecMerging().execute(ctx)
    entries = repo.merge_queue_for_project("p")
    assert len(entries) == 1


def test_spec_merging_does_not_require_git_provider():
    """Unlike MergingState (which needs git for the base-ref guard),
    SpecMerging's hand-off doesn't touch GitHub at all — the PR number
    comes from the ticket's own outcome history. A StateContext built
    without ``git`` must still succeed."""
    ctx, _repo = _ctx_with_pr(pr_number=42)
    assert ctx.git is None
    next_state = SpecMerging().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "MergeQueued"


def test_spec_merging_next_state_defensive_fallback_to_needs_help():
    """execute() only ever emits CLEAN now that the merge classifier lives
    in the coordinator, not here — but next_state() keeps a defensive
    fallback to NeedsHelp for any other outcome kind, mirroring
    MergingState's shape."""
    ctx, _repo = _ctx_with_pr(pr_number=42)
    outcome = Outcome(kind=OutcomeKind.NEEDS_FIX, confidence=OutcomeConfidence.HIGH, summary="x")
    next_state = SpecMerging().next_state(ctx, outcome)
    assert next_state is not None
    assert next_state.state_name == "NeedsHelp"
