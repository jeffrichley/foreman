"""SpecReviewState — Reviewer-on-spec.

foreman#416: CLEAN no longer merges the spec PR inline — it routes to the
new ``SpecMerging`` state, which merges with the self-heal framework.
``verify()`` keeps only the pr_number-present validation (a malformed
CLEAN fails fast here) and no longer touches git.
"""
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


def test_clean_outcome_advances_to_spec_merging_without_merging():
    """foreman#416: CLEAN routes to SpecMerging (NOT Implementing) and
    does NOT merge the spec PR inline anymore — the merge happens
    downstream in SpecMerging with the self-heal framework."""
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
    assert next_state.state_name == "SpecMerging"
    # The spec PR is NOT merged here — that's SpecMerging's job.
    assert git.get_pr_state(project="p", pr_number=42).merged is False
    assert ("p", 42) not in git.merge_pr_calls
    assert repo.get_ticket(ctx.ticket.id).current_state == "SpecMerging"


def test_specreview_verify_no_longer_calls_merge_pr():
    """foreman#416: verify() must NOT call merge_pr anymore. The spec PR
    number is still persisted on this state's outcome (so SpecMerging can
    find it), but no merge happens at SpecReview."""
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
    assert git.merge_pr_calls == set()
    # The pr_number is persisted on the SpecReview outcome so SpecMerging
    # can discover it via latest_pr_number_for_ticket.
    assert repo.latest_pr_number_for_ticket(ctx.ticket.id) == 42


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
