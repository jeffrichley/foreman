"""ImplFixState — fixer-impl dispatch; CLEAN routes back to ImplReview."""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class ImplFixState(RoleDispatchState):
    """Dispatch fixer-impl on an impl-review NEEDS_FIX outcome."""

    state_name = "ImplFix"
    dispatch_priority = 3
    role = "fixer-impl"

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        """Route CLEAN back to ImplReview for re-review; else NeedsHelp/Failed."""
        from foreman.v4.states.impl_review import ImplReviewState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState

        if outcome.kind == OutcomeKind.CLEAN:
            return ImplReviewState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
