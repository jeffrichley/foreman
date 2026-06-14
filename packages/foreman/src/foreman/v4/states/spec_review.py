"""SpecReviewState — Reviewer-on-spec.

On CLEAN, the spec is approved; this state merges the spec PR before
handing control to Implementing. Merging is in verify() (not execute())
so a merge failure routes through the verify failure handler with a
distinct failure_phase.
"""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class SpecReviewState(RoleDispatchState):
    state_name = "SpecReview"
    role = "reviewer-spec"

    def verify(self, ctx: StateContext, outcome: Outcome) -> None:
        if outcome.kind != OutcomeKind.CLEAN:
            return
        pr_number = outcome.artifacts.pr_number
        if pr_number is None:
            raise ValueError(
                "Reviewer-on-spec returned CLEAN but no pr_number in artifacts"
            )
        if ctx.git is None:
            raise RuntimeError("SpecReview.verify requires git in StateContext")
        ctx.git.merge_spec_pr(project=ctx.ticket.project, pr_number=pr_number)

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.implementing import ImplementingState
        from foreman.v4.states.spec_fix import SpecFixState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState

        if outcome.kind == OutcomeKind.CLEAN:
            return ImplementingState()
        if outcome.kind == OutcomeKind.NEEDS_FIX:
            return SpecFixState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
