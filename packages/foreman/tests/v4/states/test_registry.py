"""STATE_REGISTRY — name → state factory."""

from __future__ import annotations

import pytest

from foreman.v4.states.implementing import ImplementingState
from foreman.v4.states.planning import PlanningState
from foreman.v4.states.registry import STATE_REGISTRY, build_state


def test_registry_contains_all_states() -> None:
    expected = {
        "Queued",
        "Planning",
        "SpecReview",
        "SpecFix",
        "Implementing",
        "ImplReview",
        "ImplFix",
        # foreman#418: parked state for an approved impl PR awaiting
        # human merge (when auto_merge_impl is False).
        "ImplApproved",
        "Merging",
        "Done",
        "Failed",
        "NeedsHelp",
    }
    assert set(STATE_REGISTRY) == expected


def test_build_state_returns_impl_approved() -> None:
    from foreman.v4.states.impl_approved import ImplApprovedState

    assert isinstance(build_state("ImplApproved"), ImplApprovedState)


def test_build_state_returns_correct_instance() -> None:
    assert isinstance(build_state("Planning"), PlanningState)
    assert isinstance(build_state("Implementing"), ImplementingState)


def test_unknown_state_raises() -> None:
    with pytest.raises(KeyError):
        build_state("NotAState")
