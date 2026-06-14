"""ImplReviewState — reviewer-impl dispatch; CLEAN advances to Merging."""
from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class ImplReviewState(RoleDispatchState):
    state_name = "ImplReview"
    role = "reviewer-impl"

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.impl_fix import ImplFixState
        from foreman.v4.states.merging import MergingState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState

        if outcome.kind == OutcomeKind.CLEAN:
            return MergingState()
        if outcome.kind == OutcomeKind.NEEDS_FIX:
            return ImplFixState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
