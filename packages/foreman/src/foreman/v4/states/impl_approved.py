"""ImplApprovedState — polling-wait-then-finalize (foreman#443).

Replaces the old dead-end terminal (foreman#418) with an active polling
state. On each tick the Poller re-enqueues the ticket and this state
checks whether the human has merged the impl PR:

  merged       → close the originating issue + transition to Done.
  open         → return BLOCKED (runaway-cap-exempt per Phase 8d.18)
                 and self-loop back to ImplApproved.
  closed (not merged) → escalate to NeedsHelp with a human-readable
                 reason so the ticket doesn't poll forever on a PR
                 that will never merge.

The human still pulls the merge trigger (preserving the trust boundary
from #418); foreman adds the auto-close bookkeeping that was missing.

Design: ``merged`` is checked BEFORE ``closed`` because GitHub sets
both ``merged=True`` and ``pr.state == "closed"`` for merged PRs —
without this order the success branch would fall through to the
closed-without-merge NeedsHelp branch.

BLOCKED rows are exempt from the runaway-defense cap
(``count_consecutive_same_state`` skips them, Phase 8d.18), so a
ticket may wait hours or days for a human merge without falsely
escalating. A single cheap ``get_pr_state`` API call per tick is the
only cost — it frees the worker slot immediately (no long-poll).
"""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeArtifacts, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import MissingPRNumberError
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.merge_helper import close_originating_issue


class ImplApprovedState(TicketState):
    """Polling wait: impl approved, detecting human merge."""

    state_name = "ImplApproved"

    def _pr_number_for(self, ctx: StateContext) -> int:
        """Resolve the impl PR number from the ticket's outcome history.

        Mirrors :meth:`MergingState._pr_number_for` — both states read
        ``latest_pr_number_for_ticket``, which scans the journal newest-first
        and returns the first ``artifacts.pr_number`` it finds.
        """
        pr = ctx.repo.latest_pr_number_for_ticket(ctx.ticket.id)
        if pr is None:
            raise MissingPRNumberError(
                f"ImplApprovedState for ticket {ctx.ticket.id} has no PR number "
                "in any prior state outcome"
            )
        return pr

    def execute(self, ctx: StateContext) -> Outcome:
        if ctx.git is None:
            raise RuntimeError("ImplApprovedState requires git in StateContext")
        pr_number = self._pr_number_for(ctx)

        state = ctx.git.get_pr_state(
            project=ctx.ticket.project, pr_number=pr_number,
        )

        # Check merged FIRST — GitHub sets both merged=True and
        # pr.state=="closed" for merged PRs; the merged branch must win.
        if state.merged:
            close_originating_issue(ctx)
            return Outcome(
                kind=OutcomeKind.CLEAN,
                confidence=OutcomeConfidence.HIGH,
                summary="impl PR merged by human; closing originating issue",
                artifacts=OutcomeArtifacts(pr_number=pr_number),
            )

        if state.closed:
            # PR closed without being merged (human declined / foreman reset).
            return Outcome(
                kind=OutcomeKind.NEEDS_HELP,
                confidence=OutcomeConfidence.HIGH,
                summary=(
                    f"impl PR #{pr_number} was closed without merging; "
                    "human intervention required to re-open or replace it"
                ),
                artifacts=OutcomeArtifacts(pr_number=pr_number),
            )

        # PR is still open — the human hasn't merged yet.
        # Return BLOCKED so count_consecutive_same_state skips this row
        # (Phase 8d.18) and the runaway cap is not tripped.
        return Outcome(
            kind=OutcomeKind.BLOCKED,
            confidence=OutcomeConfidence.HIGH,
            summary="awaiting human merge of impl PR",
            artifacts=OutcomeArtifacts(pr_number=pr_number),
        )

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.terminal import DoneState, NeedsHelpState

        if outcome.kind == OutcomeKind.CLEAN:
            return DoneState()
        if outcome.kind == OutcomeKind.BLOCKED:
            return ImplApprovedState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        # Defensive fall-through: any unexpected outcome kind routes to
        # NeedsHelp so an operator can sort it out.
        return NeedsHelpState()
