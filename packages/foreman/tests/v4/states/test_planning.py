"""PlanningState — Planner role; CLEAN → SpecReview, NEEDS_HELP → NeedsHelp."""
from __future__ import annotations

import pytest

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.states.planning import PlanningState


@pytest.mark.parametrize(
    "kind,next_class_name",
    [
        (OutcomeKind.CLEAN, "SpecReview"),
        (OutcomeKind.NEEDS_HELP, "NeedsHelp"),
        (OutcomeKind.ERROR, "Failed"),
    ],
)
def test_next_state_branching(kind, next_class_name):
    # foreman#361: routing logic lives on ``next_state_for``; the
    # widened ``next_state`` Template Method intercepts
    # TRANSIENT_PROVIDER_ERROR and would need a real StateContext to
    # call. Routing tests stay on ``next_state_for``.
    outcome = Outcome(kind=kind, confidence=OutcomeConfidence.HIGH, summary="x")
    next_state = PlanningState().next_state_for(outcome)
    if next_class_name is None:
        assert next_state is None
    else:
        assert next_state is not None
        assert next_state.state_name == next_class_name


def test_planning_role_attribute():
    assert PlanningState.role == "planner"
