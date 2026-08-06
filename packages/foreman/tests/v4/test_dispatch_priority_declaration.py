"""Every registered state must declare its own dispatch priority (foreman#589).

These are the drift guards. The queue no longer keeps a second copy of the
state list, so the failure they protect against is a state joining
``STATE_REGISTRY`` without anyone choosing how urgently it should run.
"""

from __future__ import annotations

import pytest

from foreman.v4.state import TicketState
from foreman.v4.states.registry import (
    STATE_DISPATCH_PRIORITY,
    STATE_REGISTRY,
    validate_dispatch_priorities,
)


def test_every_registered_state_declares_its_own_priority():
    """No registered state inherits its priority from the base class."""
    inherited = [
        name for name, cls in STATE_REGISTRY.items() if "dispatch_priority" not in vars(cls)
    ]
    assert inherited == []


def test_derived_table_covers_the_whole_registry():
    """The priority map is derived, so it cannot omit a registered state."""
    assert set(STATE_DISPATCH_PRIORITY) == set(STATE_REGISTRY)


def test_validator_rejects_a_state_with_no_declared_priority():
    """Adding a state without a priority fails loudly instead of defaulting.

    This is the test that prevents recurrence: the pre-fix code absorbed such
    a state into a sentinel 99 and mis-ordered it silently.
    """

    class UndeclaredState(TicketState):
        state_name = "Undeclared"

        def execute(self, ctx):  # pragma: no cover - never invoked
            raise NotImplementedError

        def next_state(self, ctx, outcome):  # pragma: no cover - never invoked
            raise NotImplementedError

    with pytest.raises(TypeError, match="Undeclared"):
        validate_dispatch_priorities({"Undeclared": UndeclaredState})


def test_validator_accepts_an_explicit_none():
    """``None`` is a real declaration — 'never worker-dispatched', not absence."""

    class ParkedState(TicketState):
        state_name = "Parked"
        dispatch_priority = None

        def execute(self, ctx):  # pragma: no cover - never invoked
            raise NotImplementedError

        def next_state(self, ctx, outcome):  # pragma: no cover - never invoked
            raise NotImplementedError

    validate_dispatch_priorities({"Parked": ParkedState})


def test_impl_approved_outranks_every_earlier_pipeline_stage():
    """ImplApproved is top tier — the concrete regression from foreman#589."""
    approved = STATE_DISPATCH_PRIORITY["ImplApproved"]
    assert approved is not None
    for earlier in ("ImplReview", "Implementing", "ImplFix", "SpecReview", "Planning", "Queued"):
        stage = STATE_DISPATCH_PRIORITY[earlier]
        assert stage is not None
        assert approved < stage, f"ImplApproved must outrank {earlier}"


def test_priorities_descend_with_pipeline_depth():
    """The table encodes 'most-done first' end to end."""
    order = [
        "Queued",
        "Planning",
        "SpecReview",
        "Implementing",
        "ImplReview",
        "ImplApproved",
    ]
    priorities = [STATE_DISPATCH_PRIORITY[name] for name in order]
    assert all(p is not None for p in priorities)
    assert priorities == sorted(priorities, reverse=True), dict(zip(order, priorities, strict=True))


def test_terminal_and_coordinator_states_are_undispatched():
    """Terminal and coordinator-driven states declare None, not a large int."""
    for name in ("Done", "Failed", "NeedsHelp", "MergeQueued"):
        assert STATE_DISPATCH_PRIORITY[name] is None
