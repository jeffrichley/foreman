"""SpecMerging — merge the spec PR; mirror MergingState minus impl bits.

foreman#416. The spec-PR merge moved out of ``SpecReviewState.verify()``
(where a BEHIND spec PR hit HTTP 405 and escalated — agent_core#190) into
this dedicated state, so it gets the same self-heal framework as the impl
merge. Differences from MergingState: no base-ref guard, no issue close,
and CLEAN routes onward to Implementing (not Done).
"""
from __future__ import annotations

import datetime as dt

from foreman.v4.git_provider import FakeGitProvider, PRState
from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.spec_merging import SpecMerging


def _seed_prior_outcome(
    repo: InMemoryTicketRepository, ticket_id: int, pr_number: int
) -> None:
    """Seed the prior SpecReview CLEAN outcome carrying the spec PR number,
    mirroring what SpecReviewState writes before routing to SpecMerging."""
    prior = repo.open_state_instance(
        ticket_id=ticket_id, state_name="SpecReview", sequence=0,
        now=dt.datetime(2026, 6, 13),
    )
    repo.mark_execute_completed(
        prior.id, now=dt.datetime(2026, 6, 13),
        outcome_kind=OutcomeKind.CLEAN,
        outcome_payload={"artifacts": {"pr_number": pr_number}},
        next_state="SpecMerging",
    )
    repo.close_state_instance(prior.id, now=dt.datetime(2026, 6, 13))


def _ctx_with_pr(
    pr_number: int = 42, *, pr_state: PRState,
) -> tuple[StateContext, InMemoryTicketRepository, FakeGitProvider]:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(
        project="p", issue_number=1, now=dt.datetime(2026, 6, 13),
    )
    repo.set_ticket_state(ticket.id, "SpecMerging", now=dt.datetime(2026, 6, 13))
    _seed_prior_outcome(repo, ticket.id, pr_number)
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="SpecMerging", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    git = FakeGitProvider()
    git.set_pr_state(project="p", pr_number=pr_number, state=pr_state)
    ctx = StateContext(
        ticket=repo.get_ticket(ticket.id), instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13), git=git,
    )
    return ctx, repo, git


def test_spec_merging_state_name():
    assert SpecMerging.state_name == "SpecMerging"


def test_mergeable_spec_pr_merges_and_advances_to_implementing():
    ctx, _repo, git = _ctx_with_pr(
        pr_state=PRState(
            merged=False, mergeable=True, ci_passing=True, base_ref="main",
            mergeable_state="clean",
        ),
    )
    next_state = SpecMerging().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Implementing"
    assert ("p", 42) in git.merge_pr_calls
    assert git.get_pr_state(project="p", pr_number=42).merged is True
    # Spec merge does NOT close the issue.
    assert git.closed_issues == set()


def test_already_merged_spec_pr_advances_to_implementing_without_merge_call():
    ctx, _repo, git = _ctx_with_pr(
        pr_state=PRState(
            merged=True, mergeable=True, ci_passing=True, base_ref="main",
            mergeable_state="clean",
        ),
    )
    next_state = SpecMerging().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Implementing"
    assert ("p", 42) not in git.merge_pr_calls
    assert git.closed_issues == set()


def test_behind_spec_pr_updates_branch_and_blocks():
    """The agent_core#190 fix: a BEHIND spec PR self-heals via
    update_branch instead of 405-ing into NeedsHelp. The state stays in
    SpecMerging (self-loop) for the next poll."""
    ctx, _repo, git = _ctx_with_pr(
        pr_state=PRState(
            merged=False, mergeable=False, ci_passing=True, base_ref="main",
            mergeable_state="behind",
        ),
    )
    next_state = SpecMerging().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "SpecMerging"
    assert git.update_branch_calls == [("p", 42)]
    assert ("p", 42) not in git.merge_pr_calls


def test_ci_pending_spec_pr_blocks_without_update_branch():
    ctx, _repo, git = _ctx_with_pr(
        pr_state=PRState(
            merged=False, mergeable=False, ci_passing=False, base_ref="main",
            mergeable_state="blocked",
        ),
    )
    next_state = SpecMerging().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "SpecMerging"
    assert git.update_branch_calls == []
    assert ("p", 42) not in git.merge_pr_calls


def test_spec_merging_has_no_base_ref_guard():
    """Spec PRs legitimately target the project dev branch but the spec
    flow has no dev_base_branch expectation; a non-main base must NOT
    block the spec merge (unlike MergingState's impl guard)."""
    ctx, _repo, git = _ctx_with_pr(
        pr_state=PRState(
            merged=False, mergeable=True, ci_passing=True,
            base_ref="some-other-branch", mergeable_state="clean",
        ),
    )
    next_state = SpecMerging().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Implementing"
    assert ("p", 42) in git.merge_pr_calls
