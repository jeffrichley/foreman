"""SpecFix, ImplReview, ImplFix — uniform-shape role-dispatch states."""

from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.config import ProjectConfig
from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.impl_fix import ImplFixState
from foreman.v4.states.impl_review import ImplReviewState
from foreman.v4.states.spec_fix import SpecFixState


def _o(kind: OutcomeKind) -> Outcome:
    return Outcome(kind=kind, confidence=OutcomeConfidence.HIGH, summary="x")


def _project_config(*, auto_merge_impl: bool = False) -> ProjectConfig:
    return ProjectConfig(
        name="p",
        repo="o/p",
        local_clone_path="/tmp/p",
        auto_merge_impl=auto_merge_impl,
    )


def _ctx(
    *,
    project_configs: dict[str, ProjectConfig],
) -> tuple[StateContext, FakeGitProvider]:
    """Build a StateContext for an ImplReview ticket with the given
    per-project config map and a FakeGitProvider so tests can assert
    ``merge_pr`` is never called by the gating decision."""
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(ticket.id, "ImplReview", now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="ImplReview",
        sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    git = FakeGitProvider()
    ctx = StateContext(
        ticket=repo.get_ticket(ticket.id),
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        git=git,
        project_configs=project_configs,
    )
    return ctx, git


@pytest.mark.parametrize(
    "state_class,role,clean_next,needs_help_next",
    [
        (SpecFixState, "fixer-spec", "SpecReview", "NeedsHelp"),
        (ImplFixState, "fixer-impl", "ImplReview", "NeedsHelp"),
    ],
)
def test_fixer_state_routing(state_class, role, clean_next, needs_help_next):
    # foreman#361: routing logic lives on ``next_state_for``; the
    # widened ``next_state`` Template Method intercepts
    # TRANSIENT_PROVIDER_ERROR and would need a real StateContext to
    # call. These tests exercise the underlying routing so they
    # stay on ``next_state_for``.
    state = state_class()
    assert state.role == role
    assert state.next_state_for(_o(OutcomeKind.CLEAN)).state_name == clean_next
    assert state.next_state_for(_o(OutcomeKind.NEEDS_HELP)).state_name == needs_help_next


def test_impl_review_state_routing():
    """foreman#418: non-CLEAN routing is unchanged. CLEAN routing is
    gated by ``auto_merge_impl`` and exercised in the gating tests below
    via ``next_state`` (which needs ``ctx`` to read the flag)."""
    state = ImplReviewState()
    assert state.role == "reviewer-impl"
    assert state.next_state_for(_o(OutcomeKind.NEEDS_FIX)).state_name == "ImplFix"
    assert state.next_state_for(_o(OutcomeKind.NEEDS_HELP)).state_name == "NeedsHelp"


def test_error_outcome_routes_to_failed():
    for cls in (SpecFixState, ImplFixState):
        next_state = cls().next_state_for(_o(OutcomeKind.ERROR))
        assert next_state.state_name == "Failed"


# ---------------------------------------------------------------------------
# foreman#418: impl-merge gate
#
# On a CLEAN impl review, ImplReviewState routes to MergingState only when
# the ticket's project has auto_merge_impl=True; otherwise it parks at
# ImplApproved for human merge. Default-safe: a missing project config or
# auto_merge_impl=False both park.
# ---------------------------------------------------------------------------


def test_impl_review_clean_parks_at_impl_approved_by_default():
    """auto_merge_impl=False (default) → CLEAN parks at ImplApproved and
    merge_pr is NEVER called."""
    ctx, git = _ctx(project_configs={"p": _project_config(auto_merge_impl=False)})
    next_state = ImplReviewState().next_state(ctx, _o(OutcomeKind.CLEAN))
    assert next_state is not None
    assert next_state.state_name == "ImplApproved"
    assert git.merge_pr_calls == set()


def test_impl_review_clean_merges_when_opted_in():
    """auto_merge_impl=True → CLEAN routes to Merging (historic behavior,
    opt-in)."""
    ctx, _git = _ctx(project_configs={"p": _project_config(auto_merge_impl=True)})
    next_state = ImplReviewState().next_state(ctx, _o(OutcomeKind.CLEAN))
    assert next_state is not None
    assert next_state.state_name == "Merging"


def test_impl_review_clean_parks_when_project_config_missing():
    """Safe default: project absent from the project_configs map → park at
    ImplApproved, never auto-merge."""
    ctx, git = _ctx(project_configs={})
    next_state = ImplReviewState().next_state(ctx, _o(OutcomeKind.CLEAN))
    assert next_state is not None
    assert next_state.state_name == "ImplApproved"
    assert git.merge_pr_calls == set()


def test_impl_review_non_clean_routing_via_next_state():
    """The gating override must preserve NEEDS_FIX → ImplFix and
    NEEDS_HELP → NeedsHelp routing through ``next_state``."""
    ctx, _git = _ctx(project_configs={"p": _project_config()})
    assert ImplReviewState().next_state(ctx, _o(OutcomeKind.NEEDS_FIX)).state_name == "ImplFix"
    ctx2, _git2 = _ctx(project_configs={"p": _project_config()})
    assert ImplReviewState().next_state(ctx2, _o(OutcomeKind.NEEDS_HELP)).state_name == "NeedsHelp"
