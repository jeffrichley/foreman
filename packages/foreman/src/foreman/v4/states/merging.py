"""MergingState — direct ``pr.merge()`` on the impl PR.

The only state in v4 whose execute() doesn't dispatch a role. The Worker
already opened the impl PR; here we wait for GitHub to report the PR as
mergeable + CI passing, then call ``pr.merge()`` ourselves.

Phase 8d.19 collapsed the previous MergeQueue-based path (enqueue → poll
verdict → MERGED/REJECTED/PENDING) into a single direct merge call.
Algokit#23's 2026-06-15 dogfood proved MergingState looped on PENDING
forever because algokit doesn't have MergeQueue configured — most repos
won't. The minimum-fix design decision: same merge mechanism on every
project, until granular ``mergeable_state`` handling lands (foreman#317).

Outcomes
--------
CLEAN
    Either the PR is already merged externally OR we just merged it.
    In BOTH sub-branches the originating GitHub issue is also closed
    (Phase 8d.20) so the loop's terminal state propagates back to the
    issue tracker — without that, algokit#23's 2026-06-16 dogfood
    reached Done via pr.merge() but left the issue OPEN. Routes to Done.
BLOCKED
    GitHub says the PR isn't yet mergeable + CI-green. Stay in
    MergingState; Poller picks it up next tick. Phase 8d.18's
    BLOCKED-exemption keeps the retry cap from tripping on this
    legitimate polling. ``close_issue`` is NOT called on this branch —
    closing on every poll tick would prematurely close issues whose
    impl PR isn't actually merged yet.

Granular failure handling (CI failed → ImplFix, dirty → ImplFix, blocked
by review, etc.) is foreman#317. This task ships the minimum 3-branch
shape that lets the chain reach Done on the happy path.

The rebase case (PR base advanced while we were in this state) is
operationally sidestepped by ``max_in_flight = 1`` for now — foreman#316
tracks the proper fix.
"""

from __future__ import annotations

from foreman.v4.outcome import (
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)
from foreman.v4.repository import MissingPRNumberError
from foreman.v4.state import StateContext, TicketState


class MergingState(TicketState):
    state_name = "Merging"

    def _pr_number_for(self, ctx: StateContext) -> int:
        pr = ctx.repo.latest_pr_number_for_ticket(ctx.ticket.id)
        if pr is None:
            raise MissingPRNumberError(
                f"MergingState for ticket {ctx.ticket.id} has no PR number "
                "in any prior state outcome"
            )
        return pr

    def execute(self, ctx: StateContext) -> Outcome:
        if ctx.git is None:
            raise RuntimeError("MergingState requires git in StateContext")
        pr_number = self._pr_number_for(ctx)
        state = ctx.git.get_pr_state(
            project=ctx.ticket.project, pr_number=pr_number,
        )
        if state.merged:
            # Already-merged branch: close the originating issue too.
            # Idempotent at the REST API level — if an external merge
            # also closed the issue, this is a no-op.
            ctx.git.close_issue(
                project=ctx.ticket.project,
                issue_number=ctx.ticket.issue_number,
            )
            return Outcome(
                kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
                summary="impl PR already merged; issue closed",
                artifacts=OutcomeArtifacts(pr_number=pr_number),
            )
        if state.mergeable and state.ci_passing:
            ctx.git.merge_pr(
                project=ctx.ticket.project, pr_number=pr_number,
            )
            # Close issue AFTER the merge confirms — never before. A
            # premature close on a still-unmerged PR would orphan the
            # issue from its work; this ordering ties the close to the
            # merge succeeding.
            ctx.git.close_issue(
                project=ctx.ticket.project,
                issue_number=ctx.ticket.issue_number,
            )
            return Outcome(
                kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
                summary="impl PR merged; issue closed",
                artifacts=OutcomeArtifacts(pr_number=pr_number),
            )
        return Outcome(
            kind=OutcomeKind.BLOCKED, confidence=OutcomeConfidence.HIGH,
            summary="impl PR not yet mergeable (CI pending or merge conflict)",
            artifacts=OutcomeArtifacts(pr_number=pr_number),
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.terminal import DoneState, NeedsHelpState
        if outcome.kind == OutcomeKind.CLEAN:
            return DoneState()
        if outcome.kind == OutcomeKind.BLOCKED:
            return MergingState()
        # Defensive fall-through: execute() only ever returns CLEAN or
        # BLOCKED today, but any other outcome kind from a future
        # refactor (or a misbehaving subclass) should route to
        # NeedsHelp so an operator can sort it out — never silently
        # land on Failed.
        return NeedsHelpState()
