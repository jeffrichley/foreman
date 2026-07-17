"""SpecMerging — merge the approved spec PR before Implementing.

foreman#416. The spec-PR merge used to live in
``SpecReviewState.verify()``: on CLEAN the reviewer-approved spec PR was
merged inline, then the ticket advanced to Implementing. A spec PR that was
``BEHIND`` (its base advanced while it sat in review) made that inline
``merge_pr`` call return HTTP 405 → verify failure → escalate to NeedsHelp.
That forced a manual operator rescue of agent_core#190.

This dedicated state mirrors ``MergingState`` so the spec merge gets the
same self-heal framework (``attempt_merge`` + ``MERGE_HEALERS``): a BEHIND
spec PR now issues ``update_branch`` and re-polls instead of 405-ing.

Differences from MergingState (impl merge)
------------------------------------------
* No base-ref guard — the foreman#357 ``dev_base_branch`` check is specific
  to impl PRs (the bug class was a wrong-base IMPL PR). The spec PR's base
  isn't constrained by that config, so ``pre_merge_guard`` is omitted.
* No issue close — the originating issue is closed when the IMPL PR merges
  (MergingState), not when the spec PR merges. ``on_merge_success`` is a
  no-op.
* On success the ticket advances to ``Implementing`` (not Done) — the spec
  is now on the dev branch and the Worker can start.

Outcomes / routing
------------------
CLEAN     → Implementing (spec merged; start the build).
BLOCKED   → SpecMerging (self-loop; Poller re-polls — heal-then-wait or
            wait-for-CI). BLOCKED rows are retry-cap-exempt (Phase 8d.18),
            and the heal-action bound in ``attempt_merge`` catches
            pathological base-churn.
NEEDS_HELP → NeedsHelp (a healer escalated, or the heal bound tripped).
NEEDS_FIX  → NeedsHelp (foreman#317: a dirty/CI-failed spec PR). Unlike
            MergingState, this does NOT route to a Fixer — there is no
            SpecFix role yet, so it escalates to a human. The symmetric
            SpecMerging→SpecFix option is tracked in foreman#548.

Not terminal — it transitions onward, so it is deliberately NOT in
``state._TERMINAL_STATE_NAMES`` / ``poller._TERMINAL_STATES``; it keeps
being polled exactly like MergingState.
"""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeKind
from foreman.v4.repository import MissingPRNumberError
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.merge_helper import attempt_merge


class SpecMerging(TicketState):
    """Merge the approved spec PR, self-healing a BEHIND base via MERGE_HEALERS."""

    state_name = "SpecMerging"

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
        """Attempt the spec-PR merge; no base-ref guard or issue-close.

        See the module docstring for why those differ from MergingState.
        """
        if ctx.git is None:
            raise RuntimeError("SpecMerging requires git in StateContext")
        pr_number = self._pr_number_for(ctx)
        # No base-ref guard, no issue close — see module docstring.
        return attempt_merge(
            ctx,
            pr_number=pr_number,
            on_merge_success=lambda: None,
        )

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        """Route CLEAN to Implementing, BLOCKED to a self-loop, else NeedsHelp."""
        from foreman.v4.states.implementing import ImplementingState
        from foreman.v4.states.terminal import NeedsHelpState

        if outcome.kind == OutcomeKind.CLEAN:
            return ImplementingState()
        if outcome.kind == OutcomeKind.BLOCKED:
            return SpecMerging()
        if outcome.kind == OutcomeKind.NEEDS_HELP:
            return NeedsHelpState()
        # foreman#317: attempt_merge can now emit NEEDS_FIX for a dirty
        # (merge-conflict) or CI-failed spec PR. Unlike MergingState (impl
        # PRs), spec PRs don't route NEEDS_FIX to a Fixer — there is no
        # SpecFix role yet, so this escalates to a human rather than
        # attempting an auto-fix. The symmetric SpecMerging→SpecFix option
        # is tracked in foreman#548. Made explicit (not left to the
        # defensive fall-through below) so this is a deliberate choice, not
        # an accident of an unhandled outcome kind.
        if outcome.kind == OutcomeKind.NEEDS_FIX:
            return NeedsHelpState()
        # Defensive fall-through (mirrors MergingState): any other outcome
        # routes to NeedsHelp so an operator sorts it out — never silently
        # land on Failed.
        return NeedsHelpState()
