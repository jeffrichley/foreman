"""MergingState — placeholder for Task 3.8."""
from __future__ import annotations

from foreman.v4.outcome import Outcome
from foreman.v4.state import StateContext, TicketState


class MergingState(TicketState):
    state_name = "Merging"

    def execute(self, ctx: StateContext) -> Outcome:
        raise NotImplementedError("filled in at Task 3.8")

    def next_state(self, outcome: Outcome) -> TicketState | None:
        raise NotImplementedError("filled in at Task 3.8")
