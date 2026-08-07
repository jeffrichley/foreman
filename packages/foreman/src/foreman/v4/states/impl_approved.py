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

import datetime as dt

from foreman.v4.outcome import Outcome, OutcomeArtifacts, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import MissingPRNumberError
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.merge_helper import close_originating_issue

# foreman#583: back off human-gated polling to 5-minute intervals.
# ImplApproved waits on a human to merge, so polling it at the dispatch
# cadence (~36s) spends a role instance every 36 seconds to re-ask a
# question only a person can answer. Measured 2026-08-07: 65 of the day's
# 136 state instances — 48% of all foreman activity — were ImplApproved
# polls, against 4 completed tickets.
#
# 36s -> 300s is an 8.3x reduction: ~2,400/day per waiting ticket becomes
# ~288/day. Still not free, which is why the interval is a named constant
# rather than a literal — raising it further is a one-line change if the
# residual volume turns out to matter.
HUMAN_POLL_INTERVAL_SECONDS: int = 300


class ImplApprovedState(TicketState):
    """Polling wait: impl approved, detecting human merge."""

    state_name = "ImplApproved"
    # foreman#589: top tier. ImplApproved is strictly more done than
    # ImplReview and is a fast non-role poll into Merging — same reasoning
    # as SpecMerging (#416). Previously absent from the queue's table and
    # so dispatched LAST, behind freshly-Queued work, which starved the
    # most-done ticket whenever the global cap was saturated.
    dispatch_priority = 1

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
        """Poll the impl PR's merge state and return CLEAN/BLOCKED/NEEDS_HELP.

        Checks ``merged`` before ``closed`` since GitHub sets both flags on
        a merged PR — see the module docstring for the full outcome table.
        """
        if ctx.git is None:
            raise RuntimeError("ImplApprovedState requires git in StateContext")
        pr_number = self._pr_number_for(ctx)

        state = ctx.git.get_pr_state(
            project=ctx.ticket.project,
            pr_number=pr_number,
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
        """Route the polling outcome to Done, a self-loop, or NeedsHelp.

        On BLOCKED (human hasn't merged yet), stamps ``next_action_at`` so the
        Poller defers the next poll by ``HUMAN_POLL_INTERVAL_SECONDS`` —
        reducing busy-wait from one role instance per tick to ~25/day (foreman#583).

        On CLEAN and NEEDS_HELP, clears any pending suspension so the resolved
        ticket is not held in limbo by a stale ``next_action_at`` value.
        """
        from foreman.v4.states.terminal import DoneState, NeedsHelpState

        if outcome.kind == OutcomeKind.CLEAN:
            ctx.repo.clear_next_action_at(ctx.ticket.id)
            return DoneState()
        if outcome.kind == OutcomeKind.BLOCKED:
            ctx.repo.set_next_action_at(
                ctx.ticket.id,
                when=ctx.clock() + dt.timedelta(seconds=HUMAN_POLL_INTERVAL_SECONDS),
            )
            return ImplApprovedState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            ctx.repo.clear_next_action_at(ctx.ticket.id)
            return NeedsHelpState()
        # Defensive fall-through: any unexpected outcome kind routes to
        # NeedsHelp so an operator can sort it out.
        return NeedsHelpState()
