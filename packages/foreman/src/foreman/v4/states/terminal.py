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

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return None


class DoneState(_TerminalState):
    state_name = "Done"


class FailedState(_TerminalState):
    state_name = "Failed"


class NeedsHelpState(_TerminalState):
    state_name = "NeedsHelp"
