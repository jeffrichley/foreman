"""SpecReviewState — Reviewer-on-spec.

On CLEAN, the spec is approved; the ticket advances to ``SpecMerging``,
which merges the spec PR with the self-heal framework (foreman#416).

The merge USED to happen here in ``verify()``. That inline ``merge_pr``
call returned HTTP 405 when the spec PR was ``BEHIND`` (its base advanced
during review), routing through the verify failure handler to NeedsHelp —
the agent_core#190 manual-rescue incident. Moving the merge into the
dedicated ``SpecMerging`` state lets a BEHIND spec PR self-heal via
``update_branch`` instead of escalating. ``verify`` no longer touches git.

The CLEAN outcome still carries ``artifacts.pr_number`` (the spec PR),
which ``mark_execute_completed`` persists on this state's instance row, so
``SpecMerging._pr_number_for`` (via ``latest_pr_number_for_ticket``) can
discover the spec PR — exactly how ``MergingState`` finds the impl PR.
"""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class SpecReviewState(RoleDispatchState):
    """Dispatch reviewer-spec; a CLEAN spec advances to SpecMerging."""

    state_name = "SpecReview"
    dispatch_priority = 4
    role = "reviewer-spec"

    def verify(self, ctx: StateContext, outcome: Outcome) -> None:
        """Validate a CLEAN outcome carries the spec PR number.

        The merge moved to SpecMerging (foreman#416), but the early
        pr_number validation stays here: a CLEAN outcome with no
        ``artifacts.pr_number`` is a malformed reviewer response that must
        fail fast at SpecReview (verify failure_phase) rather than deferring
        to SpecMerging, where it would surface as a MissingPRNumberError in
        execute() with a less obvious cause. ``verify`` no longer touches
        git — the merge happens downstream in SpecMerging.
        """
        if outcome.kind != OutcomeKind.CLEAN:
            return
        if outcome.artifacts.pr_number is None:
            raise ValueError("Reviewer-on-spec returned CLEAN but no pr_number in artifacts")

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        """Route CLEAN to SpecMerging, NEEDS_FIX to SpecFix, else NeedsHelp/Failed."""
        from foreman.v4.states.spec_fix import SpecFixState
        from foreman.v4.states.spec_merging import SpecMerging
        from foreman.v4.states.terminal import FailedState, NeedsHelpState

        if outcome.kind == OutcomeKind.CLEAN:
            return SpecMerging()
        if outcome.kind == OutcomeKind.NEEDS_FIX:
            return SpecFixState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
