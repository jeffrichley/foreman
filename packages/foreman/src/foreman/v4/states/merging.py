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
from foreman.v4.states.merge_helper import attempt_merge, close_originating_issue

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
    """Merge the impl PR directly once GitHub reports it mergeable and green."""

    state_name = "Merging"

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
        """Attempt the impl-PR merge, guarded by the foreman#357 base-ref check."""
        if ctx.git is None:
            raise RuntimeError("MergingState requires git in StateContext")
        pr_number = self._pr_number_for(ctx)

        def on_merge_success() -> None:
            # Close the originating issue on BOTH success branches
            # (already-merged + just-merged). foreman#443: delegated to
            # the shared close_originating_issue helper so both MergingState
            # and ImplApprovedState call one implementation. Idempotent at
            # the REST API level — if an external merge also closed the
            # issue, this is a no-op. On the just-merged branch
            # attempt_merge calls this AFTER merge_pr confirms, so the
            # close is tied to the merge succeeding — never a premature
            # close on a still-unmerged PR.
            close_originating_issue(ctx)

        return attempt_merge(
            ctx,
            pr_number=pr_number,
            on_merge_success=on_merge_success,
            pre_merge_guard=self._base_ref_guard(ctx, pr_number),
        )

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        """Route CLEAN to Done, BLOCKED to a self-loop, else NeedsHelp."""
        from foreman.v4.states.terminal import DoneState, NeedsHelpState

        if outcome.kind == OutcomeKind.CLEAN:
            return DoneState()
        if outcome.kind == OutcomeKind.BLOCKED:
            return MergingState()
        # foreman#357: explicit NEEDS_HELP branch — the base-ref guard
        # emits this when the impl PR's base doesn't match the project's
        # configured dev_base_branch. Made explicit (rather than relying
        # on the fall-through below) so the routing intent is grep-able
        # from the new test names.
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        # foreman#317 (C1): attempt_merge emits NEEDS_FIX for a CI-failed
        # or dirty (merge-conflict) impl PR — routes to the Fixer instead
        # of looping BLOCKED forever or landing an operator in NeedsHelp
        # for something the Fixer can resolve on its own.
        if outcome.kind == OutcomeKind.NEEDS_FIX:
            from foreman.v4.states.impl_fix import ImplFixState

            return ImplFixState()
        # Defensive fall-through: CLEAN, BLOCKED, NEEDS_HELP, and
        # NEEDS_FIX all have explicit branches above; any other outcome
        # kind from a future refactor (or a misbehaving subclass) should
        # route to NeedsHelp so an operator can sort it out — never
        # silently land on Failed.
        return NeedsHelpState()
