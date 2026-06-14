"""PlanningState — placeholder for Task 3.4."""
from __future__ import annotations

from foreman.v4.outcome import Outcome
from foreman.v4.state import StateContext, TicketState


class PlanningState(TicketState):
    state_name = "Planning"

    def execute(self, ctx: StateContext) -> Outcome:
        raise NotImplementedError("filled in at Task 3.4")

    def next_state(self, outcome: Outcome) -> TicketState | None:
        raise NotImplementedError("filled in at Task 3.4")
