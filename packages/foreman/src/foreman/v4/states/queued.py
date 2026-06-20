"""QueuedState — entry hop. New tickets land here; advance to Planning."""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.state import StateContext, TicketState


class QueuedState(TicketState):
    state_name = "Queued"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary="queued; advancing to planning",
        )

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        # Late import to keep the states package import-cycle-free.
        from foreman.v4.states.planning import PlanningState
        return PlanningState()
