"""ImplementingState — Worker; BLOCKED stays in state pending Poller re-check."""
from __future__ import annotations

import pytest

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.states.implementing import ImplementingState


def _o(kind: OutcomeKind) -> Outcome:
    return Outcome(kind=kind, confidence=OutcomeConfidence.HIGH, summary="x")


@pytest.mark.parametrize(
    "kind,expected_state_name",
    [
        (OutcomeKind.CLEAN, "ImplReview"),
        (OutcomeKind.BLOCKED, "Implementing"),
        (OutcomeKind.NEEDS_HELP, "NeedsHelp"),
        (OutcomeKind.ERROR, "Failed"),
    ],
)
def test_routing(kind, expected_state_name):
    next_state = ImplementingState().next_state(_o(kind))
    assert next_state is not None
    assert next_state.state_name == expected_state_name


def test_blocked_returns_new_implementing_instance():
    """Same logical state, new instance — Poller picks it up next tick."""
    state = ImplementingState()
    next_state = state.next_state(_o(OutcomeKind.BLOCKED))
    assert isinstance(next_state, ImplementingState)
    assert next_state is not state


def test_role_attribute():
    assert ImplementingState.role == "worker"
