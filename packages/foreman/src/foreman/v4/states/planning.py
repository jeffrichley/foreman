"""PlanningState — dispatch Planner; CLEAN → SpecReview; else terminal-ish."""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class PlanningState(RoleDispatchState):
    state_name = "Planning"
    role = "planner"

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.spec_review import SpecReviewState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState
        if outcome.kind == OutcomeKind.CLEAN:
            return SpecReviewState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
