"""SpecFixState — fixer-spec dispatch; CLEAN routes back to SpecReview."""
from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class SpecFixState(RoleDispatchState):
    """Dispatch fixer-spec on a spec-review NEEDS_FIX outcome."""

    state_name = "SpecFix"
    role = "fixer-spec"

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        """Route CLEAN back to SpecReview for re-review; else NeedsHelp/Failed."""
        from foreman.v4.states.spec_review import SpecReviewState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState

        if outcome.kind == OutcomeKind.CLEAN:
            return SpecReviewState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
