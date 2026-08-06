"""MergingState — guards the impl PR's base ref, then hands it to the merge queue.

The only state in v4 whose execute() doesn't dispatch a role. The Worker
already opened the impl PR; historically this state waited for GitHub to
report it mergeable + CI passing and called ``pr.merge()`` itself
(Phase 8d.19 collapsed the old MergeQueue-based enqueue/poll path into that
single direct merge call — see foreman#317/#357 history below for how the
classifier grew from there).

foreman#550 moved the actual merge back out — this time into a per-repo
``MergeCoordinator`` (a later foreman#550 task) rather than a role. The
merge-queue *concept* returns, but as an explicit, observable
``TicketRepository``-backed FIFO instead of the ad-hoc mechanism Phase
8d.19 replaced: multiple same-repo PRs can now be ready to merge
concurrently (once ``ProjectConfig.max_in_flight`` is raised above 1)
without racing each other, because the coordinator serializes them.
``MergingState.execute()`` now does two things only:

1. The foreman#357 base-ref guard — still enforced *before* the ticket
   ever enters the queue. A wrong-base impl PR (foreman#341 logic bug;
   foreman#347 stale-binary regression) must never even be handed to the
   coordinator.
2. The hand-off: ``ctx.repo.enqueue_merge(...)`` (idempotent — see
   ``merge_helper.enqueue_for_merge``) then route to ``MergeQueued``.

Outcomes
--------
CLEAN
    The base-ref guard passed and the impl PR was (idempotently) enqueued
    on the project's merge_queue. Routes to ``MergeQueued`` — the
    coordinator drives the ticket from there; this state does not merge,
    close the issue, or poll anything itself.
NEEDS_HELP
    The foreman#357 base-ref guard refused the hand-off (the impl PR's
    base doesn't match the project's configured ``dev_base_branch``).
    Escalates to a human; nothing is enqueued.

The merge classifier that used to live here (BLOCKED wait-for-CI, the
BehindBranchHealer, NEEDS_FIX for a dirty/CI-failed PR — shipped in
foreman#317, see ``merge_helper.attempt_merge``) still exists, unchanged,
for the coordinator to reuse from its own tick loop. It is simply no
longer invoked from this state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from foreman.v4.git_provider import PRState
from foreman.v4.outcome import (
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)
from foreman.v4.repository import MissingPRNumberError
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.merge_helper import enqueue_for_merge

logger = logging.getLogger(__name__)

#: Fallback base branch when a :class:`~foreman.v4.config.ProjectConfig`
#: has ``dev_base_branch=None``. The Worker resolves ``None`` to the
#: origin's actual default branch via ``git symbolic-ref`` in
#: ``worktree._resolve_default_branch``; ``MergingState`` has no clone
#: to probe and replicating the resolution would require a new
#: GitProvider Protocol method (which foreman#357 explicitly forbids).
#: ``"main"`` matches the de-facto default on every project this
#: orchestrator currently runs against. Operators on non-``main``
#: defaults MUST set ``dev_base_branch`` explicitly in
#: ``ProjectConfig`` — otherwise this fallback will refuse to merge
#: their PRs as the wrong base.
DEFAULT_DEV_BASE_BRANCH = "main"


class MergingState(TicketState):
    """Guard the impl PR's base ref, then hand it off to the merge coordinator's queue."""

    state_name = "Merging"
    dispatch_priority = 1

    def _pr_number_for(self, ctx: StateContext) -> int:
        pr = ctx.repo.latest_pr_number_for_ticket(ctx.ticket.id)
        if pr is None:
            raise MissingPRNumberError(
                f"MergingState for ticket {ctx.ticket.id} has no PR number "
                "in any prior state outcome"
            )
        return pr

    def _base_ref_guard(
        self,
        ctx: StateContext,
        pr_number: int,
    ) -> Callable[[PRState], Outcome | None]:
        """Build the foreman#357 base-ref guard hook for ``attempt_merge``.

        Defense-in-depth gate. Before either the already-merged
        short-circuit or the merge call, verify the PR's base ref matches
        the project's configured dev_base_branch. The Worker is supposed
        to open the impl PR against dev_base_branch; foreman#341 (logic
        bug) and foreman#347 (stale binary) both produced wrong-base impl
        PRs that MergingState happily merged into the spec branch. The
        check runs BEFORE the merged short-circuit so an externally-merged
        PR with the wrong base also surfaces as NEEDS_HELP — the rare
        "operator click-merged through the UI" case shouldn't be silently
        accepted either. When the project_configs map is empty or doesn't
        contain this ticket's project (legacy tests; misconfigured
        production), the guard logs a warning and skips — additive over
        the existing behavior.

        Returns an Outcome (NEEDS_HELP) to short-circuit, or None to
        proceed with the merge.
        """

        def guard(state: PRState) -> Outcome | None:
            project_config = ctx.project_configs.get(ctx.ticket.project)
            if project_config is None:
                logger.warning(
                    "MergingState: no project_config for project=%s; "
                    "skipping base-ref guard for ticket=%d pr=%d",
                    ctx.ticket.project,
                    ctx.ticket.id,
                    pr_number,
                )
                return None
            expected_base = project_config.dev_base_branch or DEFAULT_DEV_BASE_BRANCH
            actual_base = state.base_ref
            if not actual_base or actual_base.casefold() != expected_base.casefold():
                return Outcome(
                    kind=OutcomeKind.NEEDS_HELP,
                    confidence=OutcomeConfidence.HIGH,
                    summary=(
                        f"impl PR base {actual_base!r} does not match "
                        f"configured dev_base_branch {expected_base!r}; "
                        f"refusing to merge"
                    ),
                    artifacts=OutcomeArtifacts(pr_number=pr_number),
                    details={
                        "actual_base": actual_base,
                        "expected_base": expected_base,
                        "pr_number": pr_number,
                        "ticket_issue_number": ctx.ticket.issue_number,
                    },
                )
            return None

        return guard

    def execute(self, ctx: StateContext) -> Outcome:
        """Guard the impl PR's base ref, then enqueue it for the coordinator.

        foreman#550: the merge itself no longer happens here — see the
        module docstring. This fetches the PR's current state purely to
        feed the foreman#357 base-ref guard; if the guard passes, the PR
        is handed to ``ctx.repo``'s merge_queue via
        ``merge_helper.enqueue_for_merge``.
        """
        if ctx.git is None:
            raise RuntimeError("MergingState requires git in StateContext")
        pr_number = self._pr_number_for(ctx)

        state = ctx.git.get_pr_state(project=ctx.ticket.project, pr_number=pr_number)
        guard_outcome = self._base_ref_guard(ctx, pr_number)(state)
        if guard_outcome is not None:
            return guard_outcome

        return enqueue_for_merge(ctx, pr_number=pr_number, kind="impl")

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        """Route CLEAN (enqueued) to MergeQueued, NEEDS_HELP (bad base) to NeedsHelp."""
        from foreman.v4.states.merge_queued import MergeQueuedState
        from foreman.v4.states.terminal import NeedsHelpState

        if outcome.kind == OutcomeKind.CLEAN:
            return MergeQueuedState()
        # foreman#357: the base-ref guard emits NEEDS_HELP when the impl
        # PR's base doesn't match the project's configured
        # dev_base_branch. Made explicit (rather than relying on the
        # fall-through below) so the routing intent is grep-able.
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        # Defensive fall-through: execute() only ever emits CLEAN or
        # NEEDS_HELP now that the merge classifier (BLOCKED / NEEDS_FIX,
        # foreman#317) lives in the coordinator, not here — but any other
        # outcome kind from a future refactor (or a misbehaving subclass)
        # should still route to NeedsHelp so an operator can sort it out,
        # never silently land on Failed.
        return NeedsHelpState()
