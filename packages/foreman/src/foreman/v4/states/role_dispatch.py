"""Base class for the six role-dispatch states.

Subclass: set ``state_name``, ``role``, and override ``next_state_for(outcome)``.
That's it — the dispatch + parse + Outcome plumbing lives here once.
"""

from __future__ import annotations

from abc import abstractmethod

from foreman.v4.outcome import Outcome, parse_outcome_from_stdout
from foreman.v4.state import StateContext, TicketState


class RoleDispatchState(TicketState):
    """Common ``execute`` + ``next_state`` plumbing for role-dispatching states."""

    role: str = ""  # subclasses MUST override

    def execute(self, ctx: StateContext) -> Outcome:
        if ctx.role_dispatcher is None:
            raise RuntimeError(
                f"{self.state_name}.execute requires a role_dispatcher in StateContext"
            )
        stdout = ctx.role_dispatcher.dispatch(
            role=self.role,
            project=ctx.ticket.project,
            issue_number=ctx.ticket.issue_number,
            ticket_id=ctx.ticket.id,
        )
        return parse_outcome_from_stdout(stdout)

    @abstractmethod
    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        """Override per state. Drives the outcome-kind → next-state branching."""

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return self.next_state_for(outcome)
