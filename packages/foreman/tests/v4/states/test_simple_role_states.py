"""SpecFix, ImplReview, ImplFix — uniform-shape role-dispatch states."""
from __future__ import annotations

import pytest

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.states.impl_fix import ImplFixState
from foreman.v4.states.impl_review import ImplReviewState
from foreman.v4.states.spec_fix import SpecFixState


def _o(kind: OutcomeKind) -> Outcome:
    return Outcome(kind=kind, confidence=OutcomeConfidence.HIGH, summary="x")


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
    state = ImplReviewState()
    assert state.role == "reviewer-impl"
    assert state.next_state_for(_o(OutcomeKind.CLEAN)).state_name == "Merging"
    assert state.next_state_for(_o(OutcomeKind.NEEDS_FIX)).state_name == "ImplFix"
    assert state.next_state_for(_o(OutcomeKind.NEEDS_HELP)).state_name == "NeedsHelp"


def test_error_outcome_routes_to_failed():
    for cls in (SpecFixState, ImplReviewState, ImplFixState):
        next_state = cls().next_state_for(_o(OutcomeKind.ERROR))
        assert next_state.state_name == "Failed"
