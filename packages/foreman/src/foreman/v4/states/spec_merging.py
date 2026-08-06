"""SpecMerging — hand the approved spec PR off to the merge queue.

foreman#416. The spec-PR merge used to live in
``SpecReviewState.verify()``: on CLEAN the reviewer-approved spec PR was
merged inline, then the ticket advanced to Implementing. A spec PR that was
``BEHIND`` (its base advanced while it sat in review) made that inline
``merge_pr`` call return HTTP 405 → verify failure → escalate to NeedsHelp.
That forced a manual operator rescue of agent_core#190. A dedicated state
was carved out so the spec merge could get the same self-heal framework as
the impl merge (``attempt_merge`` + ``MERGE_HEALERS``).

foreman#550 moved the actual merge attempt out of this state entirely, into
a per-repo ``MergeCoordinator`` (a later foreman#550 task) that serializes
same-repo merges — see ``MergingState``'s module docstring for the parallel
rationale. ``SpecMerging.execute()`` now only enqueues the spec PR
(idempotently, via ``merge_helper.enqueue_for_merge``) and routes to
``MergeQueued``.

Differences from MergingState (impl merge)
------------------------------------------
* No base-ref guard — the foreman#357 ``dev_base_branch`` check is specific
  to impl PRs (the bug class was a wrong-base IMPL PR). The spec PR's base
  isn't constrained by that config, so there's nothing to check before the
  hand-off.
* No issue close — the originating issue is closed when the IMPL PR merges,
  not when the spec PR merges. That happens in the coordinator once it
  actually merges the impl PR, not here.
* No ``GitProvider`` dependency at all — the spec PR number comes from the
  ticket's own outcome history (``latest_pr_number_for_ticket``), so
  ``execute()`` never needs ``ctx.git``.

Outcomes / routing
------------------
CLEAN → MergeQueued (spec PR enqueued; the coordinator merges it and
        advances the ticket to Implementing once that succeeds).

The merge classifier that used to live here (BLOCKED wait-for-CI, the
BehindBranchHealer, NEEDS_FIX for a dirty/CI-failed spec PR — see
``merge_helper.attempt_merge``) still exists, unchanged, for the
coordinator to reuse. It is simply no longer invoked from this state.

Not terminal — it transitions onward, so it is deliberately NOT in
``state._TERMINAL_STATE_NAMES`` / ``poller._TERMINAL_STATES``.
"""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.repository import MissingPRNumberError
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.merge_helper import enqueue_for_merge


class SpecMerging(TicketState):
    """Enqueue the approved spec PR for the coordinator to merge."""

    state_name = "SpecMerging"
    # foreman#416: a fast non-role merge state — top tier so the spec PR
    # lands promptly and the build can start.
    dispatch_priority = 1

    def _pr_number_for(self, ctx: StateContext) -> int:
        """Resolve the spec PR number from the ticket's prior outcomes.

        Identical mechanism to ``MergingState._pr_number_for``: the
        reviewer-on-spec CLEAN outcome carried ``artifacts.pr_number``
        (the spec PR), which ``mark_execute_completed`` persisted on the
        SpecReview state_instance row. ``latest_pr_number_for_ticket``
        walks instances newest-first and returns the first recorded
        pr_number, so the spec PR is discoverable here.
        """
        pr = ctx.repo.latest_pr_number_for_ticket(ctx.ticket.id)
        if pr is None:
            raise MissingPRNumberError(
                f"SpecMerging for ticket {ctx.ticket.id} has no PR number "
                "in any prior state outcome"
            )
        return pr

    def execute(self, ctx: StateContext) -> Outcome:
        """Enqueue the spec PR for the coordinator; no base-ref guard, no GitProvider needed.

        See the module docstring for why those differ from MergingState.
        """
        pr_number = self._pr_number_for(ctx)
        return enqueue_for_merge(ctx, pr_number=pr_number, kind="spec")

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        """Route CLEAN (enqueued) to MergeQueued, else NeedsHelp."""
        from foreman.v4.states.merge_queued import MergeQueuedState
        from foreman.v4.states.terminal import NeedsHelpState

        if outcome.kind == OutcomeKind.CLEAN:
            return MergeQueuedState()
        # Defensive fall-through: execute() only ever emits CLEAN now that
        # the merge classifier (BLOCKED / NEEDS_FIX, foreman#317) lives in
        # the coordinator, not here — but any other outcome kind routes to
        # NeedsHelp so an operator sorts it out, never silently lands on
        # Failed. Mirrors MergingState's fallback shape.
        return NeedsHelpState()
