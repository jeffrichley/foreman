"""SpecReviewState — Reviewer-on-spec; CLEAN merges spec PR + → Implementing."""
from __future__ import annotations

import datetime as dt

from foreman.v4.git_provider import FakeGitProvider, PRState
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.state import StateContext
from foreman.v4.states.spec_review import SpecReviewState


def _ctx(*, response_stdout: str, git: FakeGitProvider):
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(ticket.id, "SpecReview", now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="SpecReview", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    dispatcher = FakeRoleDispatcher(responses={
        ("reviewer-spec", "p", 1): response_stdout,
    })
    return StateContext(
        ticket=repo.get_ticket(ticket.id), instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        role_dispatcher=dispatcher, git=git,
    ), repo


def test_clean_outcome_merges_spec_pr_and_advances_to_implementing():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    ctx, repo = _ctx(
        response_stdout=(
            'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high",'
            '"summary":"approved","artifacts":{"pr_number":42}}'
        ),
        git=git,
    )
    next_state = SpecReviewState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Implementing"
    assert git.get_pr_state(project="p", pr_number=42).merged is True
    assert repo.get_ticket(ctx.ticket.id).current_state == "Implementing"


def test_specreview_state_calls_renamed_merge_pr():
    """Phase 8d.19 rename: ``merge_spec_pr`` → ``merge_pr``. The
    SpecReviewState verify() hook calls the renamed method on the same
    code path. The recorder on FakeGitProvider proves the call landed
    on the new entry point (the old one no longer exists)."""
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    ctx, repo = _ctx(
        response_stdout=(
            'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high",'
            '"summary":"approved","artifacts":{"pr_number":42}}'
        ),
        git=git,
    )
    SpecReviewState().transition(ctx)
    assert ("p", 42) in git.merge_pr_calls


def test_needs_fix_routes_to_spec_fix_without_merge():
    git = FakeGitProvider()
    git.set_pr_state(
        project="p", pr_number=42,
        state=PRState(merged=False, mergeable=True, ci_passing=True),
    )
    ctx, repo = _ctx(
        response_stdout=(
            'FOREMAN_OUTCOME:{"kind":"needs_fix","confidence":"high",'
            '"summary":"nope","artifacts":{"pr_number":42}}'
        ),
        git=git,
    )
    next_state = SpecReviewState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "SpecFix"
    assert git.get_pr_state(project="p", pr_number=42).merged is False


def test_clean_without_pr_number_routes_to_failed_via_verify():
    git = FakeGitProvider()
    ctx, repo = _ctx(
        response_stdout=(
            'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"no pr"}'
        ),
        git=git,
    )
    next_state = SpecReviewState().transition(ctx)
    assert next_state is None
    closed = repo.get_state_instance(ctx.instance.id)
    assert closed.failure_phase == "verify"


def test_role_attribute():
    assert SpecReviewState.role == "reviewer-spec"
