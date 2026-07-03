"""ImplementingState — Worker role.

On BLOCKED, the Worker has opened an impl PR but CI is still in flight.
The state advances to a fresh ImplementingState instance — same logical
state, new sequence in the journal. The Poller picks it up on the next
tick to re-check CI verdict and reinvoke the Worker if needed.
"""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class ImplementingState(RoleDispatchState):
    """Dispatch the Worker role to open or update the ticket's impl PR."""

    state_name = "Implementing"
    role = "worker"

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        """Route CLEAN to ImplReview, BLOCKED to a self-loop, else NeedsHelp/Failed."""
        from foreman.v4.states.impl_review import ImplReviewState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState

        if outcome.kind == OutcomeKind.CLEAN:
            return ImplReviewState()
        if outcome.kind == OutcomeKind.BLOCKED:
            return ImplementingState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
