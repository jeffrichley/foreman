"""Terminal states — the ticket has reached an end-of-flow point.

Done       — happy completion (impl PR merged).
Failed     — terminal failure with no human-actionable recovery (rare).
NeedsHelp  — terminal-pending-human; resume routed through `foreman resume`
             after the human resolves the issue. The state itself is just a
             holding pen — no work to do until the ticket is moved off.
"""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.state import StateContext, TicketState


class _TerminalState(TicketState):
    """Base for no-work terminals."""

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary=f"terminal: {self.state_name}",
        )

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        return None


class DoneState(_TerminalState):
    """Happy-path terminal: the impl PR merged and the ticket is complete."""

    state_name = "Done"
    # Terminal: never worker-dispatched.
    dispatch_priority = None


class FailedState(_TerminalState):
    """Terminal failure with no human-actionable recovery path."""

    state_name = "Failed"
    # Terminal: never worker-dispatched.
    dispatch_priority = None


class NeedsHelpState(_TerminalState):
    """Terminal-pending-human holding pen; resumed via ``foreman resume``."""

    state_name = "NeedsHelp"
    # Terminal: never worker-dispatched.
    dispatch_priority = None
