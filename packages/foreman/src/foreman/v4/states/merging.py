"""MergingState — enqueues impl PR into MergeQueue; polls verdict.

The only state in v4 whose execute() doesn't dispatch a role. The Worker
already opened the impl PR; here we wait for GitHub's MergeQueue verdict.

PENDING  → stay in state (new instance, Poller picks up next tick).
MERGED   → Done.
REJECTED → ImplFix (Worker fixes whatever MergeQueue caught).
"""

from __future__ import annotations

from foreman.v4.git_provider import MergeVerdict
from foreman.v4.outcome import (
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)
from foreman.v4.state import StateContext, TicketState


class MergingState(TicketState):
    state_name = "Merging"

    def _pr_number_for(self, ctx: StateContext) -> int:
        """Find the impl PR number from the ticket's most recent ExecuteCompleted outcome.

        Implementation note: walks state_instances in reverse from current
        sequence, looking for the most recent outcome_payload with an
        artifacts.pr_number set. Production wiring uses Phase 4's Repository
        query helper; Phase 3 tests stub via monkeypatch.
        """
        # Placeholder for the read; real impl uses ctx.repo's journal walk.
        # Subclassed tests override this method with the PR number directly.
        raise NotImplementedError("override or wire via ctx.repo journal walk")

    def enter(self, ctx: StateContext) -> None:
        if ctx.git is None:
            raise RuntimeError("MergingState requires git in StateContext")
        pr_number = self._pr_number_for(ctx)
        ctx.git.enqueue_merge_queue(project=ctx.ticket.project, pr_number=pr_number)

    def execute(self, ctx: StateContext) -> Outcome:
        if ctx.git is None:
            raise RuntimeError("MergingState requires git in StateContext")
        pr_number = self._pr_number_for(ctx)
        verdict = ctx.git.merge_verdict(project=ctx.ticket.project, pr_number=pr_number)
        if verdict is MergeVerdict.MERGED:
            return Outcome(
                kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
                summary="merge queue merged",
                artifacts=OutcomeArtifacts(pr_number=pr_number),
            )
        if verdict is MergeVerdict.REJECTED:
            return Outcome(
                kind=OutcomeKind.NEEDS_FIX, confidence=OutcomeConfidence.HIGH,
                summary="merge queue rejected — CI or conflict",
                artifacts=OutcomeArtifacts(pr_number=pr_number),
            )
        return Outcome(
            kind=OutcomeKind.BLOCKED, confidence=OutcomeConfidence.HIGH,
            summary="merge queue pending verdict",
            artifacts=OutcomeArtifacts(pr_number=pr_number),
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.impl_fix import ImplFixState
        from foreman.v4.states.terminal import DoneState, FailedState
        if outcome.kind == OutcomeKind.CLEAN:
            return DoneState()
        if outcome.kind == OutcomeKind.NEEDS_FIX:
            return ImplFixState()
        if outcome.kind == OutcomeKind.BLOCKED:
            return MergingState()
        return FailedState()
