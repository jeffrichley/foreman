"""QueuedState — entry hop. New tickets land here; advance to Planning."""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.state import StateContext, TicketState


class QueuedState(TicketState):
    """Entry state for newly created tickets; advances directly to Planning."""

    state_name = "Queued"
    dispatch_priority = 6

    def execute(self, ctx: StateContext) -> Outcome:
        """Return a CLEAN outcome unconditionally — Queued has no work to do."""
        return Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary="queued; advancing to planning",
        )

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        """Advance every Queued ticket straight to Planning."""
        # Late import to keep the states package import-cycle-free.
        from foreman.v4.states.planning import PlanningState

        return PlanningState()
