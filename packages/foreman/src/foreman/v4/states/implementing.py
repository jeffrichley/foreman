"""ImplementingState — placeholder for Task 3.7."""
from __future__ import annotations

from foreman.v4.outcome import Outcome
from foreman.v4.state import StateContext, TicketState


class ImplementingState(TicketState):
    state_name = "Implementing"

    def execute(self, ctx: StateContext) -> Outcome:
        raise NotImplementedError("filled in at Task 3.7")

    def next_state(self, outcome: Outcome) -> TicketState | None:
        raise NotImplementedError("filled in at Task 3.7")
