"""ImplReviewState — reviewer-impl dispatch.

On a CLEAN review, the next state is gated by the impl-merge gate
(foreman#418): ``MergingState`` only when the ticket's project has
``auto_merge_impl=True``; otherwise the ticket parks at
``ImplApprovedState`` for human merge. Default-safe — a missing project
config or ``auto_merge_impl=False`` both park.

Non-CLEAN routing (NEEDS_FIX → ImplFix, NEEDS_HELP → NeedsHelp,
else → Failed) is unchanged and lives on ``next_state_for``. The
TRANSIENT_PROVIDER_ERROR intercept + ``next_action_at`` clearing
(foreman#361) is preserved by delegating to
``RoleDispatchState.next_state`` for every outcome except CLEAN.
"""
from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class ImplReviewState(RoleDispatchState):
    state_name = "ImplReview"
    role = "reviewer-impl"

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        """Gate the CLEAN outcome on ``auto_merge_impl``; delegate the rest.

        foreman#418: a CLEAN impl review parks at ``ImplApproved`` unless
        the project explicitly opts in to ``auto_merge_impl``. Default-
        safe: if the project is missing from ``ctx.project_configs`` OR
        ``auto_merge_impl`` is False → park (never auto-merge unless
        explicitly opted in).

        Every non-CLEAN outcome is delegated to
        :meth:`RoleDispatchState.next_state` so the foreman#361
        TRANSIENT_PROVIDER_ERROR backoff intercept and the
        defensive ``clear_next_action_at`` on successful retry both
        keep running unchanged.
        """
        if outcome.kind == OutcomeKind.CLEAN:
            # foreman#361 defense-in-depth: a CLEAN outcome is a
            # successful (non-transient) outcome, so clear any stale
            # suspension — mirrors the non-transient branch of
            # RoleDispatchState.next_state, which we are bypassing here.
            ctx.repo.clear_next_action_at(ctx.ticket.id)
            from foreman.v4.states.impl_approved import ImplApprovedState
            from foreman.v4.states.merging import MergingState

            project_config = ctx.project_configs.get(ctx.ticket.project)
            if project_config is not None and project_config.auto_merge_impl:
                return MergingState()
            return ImplApprovedState()
        return super().next_state(ctx, outcome)

    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        from foreman.v4.states.impl_fix import ImplFixState
        from foreman.v4.states.merging import MergingState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState

        if outcome.kind == OutcomeKind.CLEAN:
            # Defensive only: the impl-merge gate (next_state) intercepts
            # CLEAN before delegating here, so this branch is unreachable
            # from the normal flow. Kept so a direct next_state_for(CLEAN)
            # call (legacy tests / external callers) still routes to the
            # historic Merging target rather than crashing.
            return MergingState()
        if outcome.kind == OutcomeKind.NEEDS_FIX:
            return ImplFixState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
