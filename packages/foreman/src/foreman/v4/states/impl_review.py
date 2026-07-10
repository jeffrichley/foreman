"""ImplReviewState — reviewer-impl dispatch.

On a CLEAN review, the next state is gated by the impl-merge gate
(foreman#418): ``MergingState`` only when the ticket's project has
``auto_merge_impl=True``; otherwise the ticket parks at
``ImplApprovedState`` for human merge. Default-safe — a missing project
config or ``auto_merge_impl=False`` both park.

Non-CLEAN routing (NEEDS_FIX → ImplFix, NEEDS_HELP → NeedsHelp,
else → Failed) is unchanged and lives on ``next_state_for``, which no
longer handles CLEAN at all — the gate in ``next_state`` intercepts CLEAN
(and performs the suspension-clear) before any delegation. The
TRANSIENT_PROVIDER_ERROR intercept + ``next_action_at`` clearing
(foreman#361) is preserved by delegating to
``RoleDispatchState.next_state`` for every outcome except CLEAN.
"""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.role_dispatch import RoleDispatchState


class ImplReviewState(RoleDispatchState):
    """Dispatch reviewer-impl; the CLEAN outcome is gated in ``next_state``."""

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
        """Route NEEDS_FIX/NEEDS_HELP/else — CLEAN never reaches this method.

        See the ``next_state`` docstring above for why CLEAN is intercepted
        before delegation ever gets here.
        """
        # CLEAN is intentionally NOT handled here: the impl-merge gate
        # (foreman#418) intercepts CLEAN in ``next_state`` above — which
        # also performs the suspension-clear — and never delegates a CLEAN
        # outcome down to ``next_state_for``. Handling it here too would be
        # a foot-gun (a direct caller would get an unconditional
        # MergingState and skip the suspension-clear). All non-CLEAN
        # outcomes reach here via ``RoleDispatchState.next_state``.
        from foreman.v4.states.impl_fix import ImplFixState
        from foreman.v4.states.terminal import FailedState, NeedsHelpState

        if outcome.kind == OutcomeKind.NEEDS_FIX:
            return ImplFixState()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        return FailedState()
