"""ImplApprovedState — impl PR approved, awaiting human merge (foreman#418).

The impl-merge gate's parked landing state. When a project does NOT opt
in to ``auto_merge_impl``, an approved impl PR routes here instead of
``MergingState``: foreman has done its job (the impl is reviewed and
CLEAN) but the actual merge is deferred to a human.

Mirrors :class:`~foreman.v4.states.terminal.NeedsHelpState` exactly — a
parked, non-dispatching, terminal-for-the-machine state. ``execute()``
returns a CLEAN landing Outcome (no work to do) and ``next_state()``
returns None (the state machine halts here). Like NeedsHelp it is in
``_TERMINAL_STATE_NAMES`` (so the WorkerPool won't re-enqueue and the
landing event is synthesized inline) and the Poller's terminal set (so
the Poller skips it), but it is intentionally NOT in
``repository._TERMINAL_STATES`` ({Done, Failed}) — it must stay visible
in ``list_open_tickets`` so an operator sees the ticket awaiting their
merge.
"""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.state import StateContext, TicketState


class ImplApprovedState(TicketState):
    """Parked: impl approved, awaiting human merge."""

    state_name = "ImplApproved"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary=f"terminal: {self.state_name}",
        )

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        return None
