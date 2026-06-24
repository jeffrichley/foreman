"""STATE_REGISTRY — name → state factory."""

from __future__ import annotations

import pytest

from foreman.v4.states.implementing import ImplementingState
from foreman.v4.states.planning import PlanningState
from foreman.v4.states.registry import STATE_REGISTRY, build_state
from foreman.v4.states.spec_merging import SpecMerging


def test_registry_contains_all_states() -> None:
    expected = {
        "Queued",
        "Planning",
        "SpecReview",
        "SpecFix",
        # foreman#416: SpecMerging merges the approved spec PR (moved out
        # of SpecReviewState.verify) with the self-heal framework.
        "SpecMerging",
        "Implementing",
        "ImplReview",
        "ImplFix",
        "Merging",
        "Done",
        "Failed",
        "NeedsHelp",
    }
    assert set(STATE_REGISTRY) == expected


def test_build_state_returns_correct_instance() -> None:
    assert isinstance(build_state("Planning"), PlanningState)
    assert isinstance(build_state("Implementing"), ImplementingState)
    assert isinstance(build_state("SpecMerging"), SpecMerging)


def test_unknown_state_raises() -> None:
    with pytest.raises(KeyError):
        build_state("NotAState")
