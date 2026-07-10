"""attempt_merge — shared merge-attempt logic for the two merge states.

foreman#416. ``MergingState`` (impl PR) and ``SpecMerging`` (spec PR) share
the same merge skeleton; the only differences are caller-specific success
side effects (impl closes the originating issue; spec doesn't) and the
impl-only base-ref guard (foreman#357). Both are injected as hooks so the
skeleton lives in exactly one place.

Skeleton
--------
1. ``get_pr_state``.
2. Optional ``pre_merge_guard(pr)`` — if it returns an Outcome, return it
   verbatim (the impl base-ref guard short-circuits here; spec passes None).
3. ``merged`` → ``on_merge_success()`` → CLEAN.
4. ``mergeable and ci_passing`` → ``merge_pr`` → ``on_merge_success()`` →
   CLEAN.
5. else → consult ``MERGE_HEALERS``: first applicable healer's result maps
   ``RETRY``/``PROCEED`` → BLOCKED (re-poll), ``ESCALATE`` → NEEDS_HELP. No
   applicable healer → BLOCKED (the unchanged "wait for CI" behavior).

Heal-loop bound
---------------
A normal BEHIND heals in 1–2 cycles (after ``update_branch`` the PR is no
longer behind). Pathological base-churn — the base keeps advancing faster
than the healer can catch up — would otherwise loop ``update_branch``
forever, since each heal returns BLOCKED and BLOCKED rows are
retry-cap-exempt (Phase 8d.18). The bound counts prior BLOCKED instances
of the CURRENT state on this ticket (via ``list_state_instances_for_ticket``
— the journal already records every heal-then-poll cycle as one BLOCKED
row). At/after ``MAX_HEAL_ACTIONS`` such cycles, the helper escalates to
NEEDS_HELP instead of asking a healer to act again. No new repository
method, no GitProvider counter — the existing journal is the counter, and
the mechanism works identically across InMemory / Sqlite / Postgres.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from foreman.v4.merge_healers import MERGE_HEALERS, HealResult
from foreman.v4.outcome import (
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)

if TYPE_CHECKING:
    from foreman.v4.git_provider import PRState
    from foreman.v4.state import StateContext


def close_originating_issue(ctx: StateContext) -> None:
    """Close the originating GitHub issue after a successful impl-PR merge.

    foreman#443: shared helper called by both ``MergingState``
    (``auto_merge_impl=True`` path) and ``ImplApprovedState``
    (``auto_merge_impl=False`` / human-merge-detected path). Having a single
    implementation eliminates the prior drift risk (MergingState had an
    inline lambda; ImplApprovedState had nothing).

    Idempotent — GitHub's REST API treats closing an already-closed issue as
    a no-op (HTTP 200, no state change), so calling this twice — e.g.
    when a human also closes the issue manually — never raises.
    """
    if ctx.git is None:
        raise RuntimeError("close_originating_issue requires git in StateContext")
    ctx.git.close_issue(
        project=ctx.ticket.project,
        issue_number=ctx.ticket.issue_number,
    )


#: Max heal actions (BLOCKED heal cycles) on one merge state before the
#: helper escalates a still-unhealed PR to NEEDS_HELP. Five is generous —
#: a normal BEHIND clears in 1–2 cycles; reaching five means the base is
#: churning pathologically and a human should look. NOT a runaway-defense
#: cap on the role-dispatch retry path (that's ``max_state_attempts``);
#: this is specific to the heal loop, which is otherwise unbounded because
#: BLOCKED self-loops are intentionally retry-cap-exempt.
MAX_HEAL_ACTIONS = 5


#: Marker key written into a heal-acted BLOCKED outcome's ``details`` (value
#: = the healer name). The heal bound counts ONLY rows carrying this marker.
HEAL_ACTION_DETAIL_KEY = "heal_action"


def _prior_blocked_heal_count(ctx: StateContext) -> int:
    """Count prior heal-ACTED BLOCKED instances of the current state.

    Two BLOCKED outcomes journal identically (``outcome_kind == BLOCKED``):

    * a healer acted (behind → ``update_branch``), tagged with
      ``details[HEAL_ACTION_DETAIL_KEY]``; and
    * plain wait-for-CI (no healer applied), with no such tag.

    The bound must count ONLY the first kind — counting CI-pending polls
    would falsely escalate a PR that legitimately polls BLOCKED while CI
    runs (Phase 8d.18). ``mark_execute_completed`` persists
    ``outcome.model_dump(mode="json")`` into ``outcome_payload``, so the
    marker survives in ``outcome_payload["details"]`` and is read back
    here.
    """
    rows = ctx.repo.list_state_instances_for_ticket(ctx.ticket.id)
    count = 0
    for r in rows:
        if r.state_name != ctx.instance.state_name:
            continue
        if r.outcome_kind != OutcomeKind.BLOCKED:
            continue
        details = (r.outcome_payload or {}).get("details") or {}
        if details.get(HEAL_ACTION_DETAIL_KEY):
            count += 1
    return count


def attempt_merge(
    ctx: StateContext,
    *,
    pr_number: int,
    on_merge_success: Callable[[], None],
    pre_merge_guard: Callable[[PRState], Outcome | None] | None = None,
) -> Outcome:
    """Run the shared merge skeleton; return the Outcome for the caller.

    Parameters
    ----------
    ctx:
        The state's :class:`StateContext`. ``ctx.git`` must be set.
    pr_number:
        The PR to merge (impl PR for MergingState, spec PR for SpecMerging).
    on_merge_success:
        Caller-specific side effects run on BOTH success branches
        (already-merged + just-merged) — impl closes the issue here; spec
        passes a no-op.
    pre_merge_guard:
        Optional hook (impl base-ref guard). Receives the fetched
        ``PRState``; if it returns an Outcome, ``attempt_merge`` returns
        that verbatim and does nothing else. Spec passes ``None``.
    """
    if ctx.git is None:
        raise RuntimeError("attempt_merge requires git in StateContext")

    state = ctx.git.get_pr_state(project=ctx.ticket.project, pr_number=pr_number)

    if pre_merge_guard is not None:
        guard_outcome = pre_merge_guard(state)
        if guard_outcome is not None:
            return guard_outcome

    if state.merged:
        on_merge_success()
        return Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary="PR already merged",
            artifacts=OutcomeArtifacts(pr_number=pr_number),
        )

    if state.mergeable and state.ci_passing:
        ctx.git.merge_pr(project=ctx.ticket.project, pr_number=pr_number)
        on_merge_success()
        return Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary="PR merged",
            artifacts=OutcomeArtifacts(pr_number=pr_number),
        )

    # Not merged, not mergeable+green: consult the healer registry.
    for healer in MERGE_HEALERS:
        if not healer.applies(state):
            continue
        # Heal-loop bound: a perpetually-behind PR can't loop forever. If
        # we've already taken MAX_HEAL_ACTIONS BLOCKED heal cycles on this
        # state, stop healing and escalate — the base is churning faster
        # than we can catch.
        if _prior_blocked_heal_count(ctx) >= MAX_HEAL_ACTIONS:
            return Outcome(
                kind=OutcomeKind.NEEDS_HELP,
                confidence=OutcomeConfidence.HIGH,
                summary=(
                    f"PR still {state.mergeable_state!r} after "
                    f"{MAX_HEAL_ACTIONS} heal attempts ({healer.name}); "
                    f"escalating"
                ),
                artifacts=OutcomeArtifacts(pr_number=pr_number),
                details={
                    "mergeable_state": state.mergeable_state,
                    "healer": healer.name,
                    "pr_number": pr_number,
                    "heal_attempts": _prior_blocked_heal_count(ctx),
                },
            )
        result = healer.heal(
            ctx,
            project=ctx.ticket.project,
            pr_number=pr_number,
            pr=state,
        )
        if result is HealResult.ESCALATE:
            return Outcome(
                kind=OutcomeKind.NEEDS_HELP,
                confidence=OutcomeConfidence.HIGH,
                summary=f"healer {healer.name} escalated to human",
                artifacts=OutcomeArtifacts(pr_number=pr_number),
                details={
                    "mergeable_state": state.mergeable_state,
                    "healer": healer.name,
                    "pr_number": pr_number,
                },
            )
        # RETRY / PROCEED → BLOCKED so the Poller re-evaluates next tick.
        # Tag the outcome with the heal-action marker so the bound counts
        # this cycle (and ONLY heal-acted cycles, not CI-pending polls).
        return Outcome(
            kind=OutcomeKind.BLOCKED,
            confidence=OutcomeConfidence.HIGH,
            summary=(f"healer {healer.name} acted ({state.mergeable_state!r}); re-polling"),
            artifacts=OutcomeArtifacts(pr_number=pr_number),
            details={
                HEAL_ACTION_DETAIL_KEY: healer.name,
                "mergeable_state": state.mergeable_state,
                "pr_number": pr_number,
            },
        )

    # No healer applied: the plain "wait for CI" case — unchanged.
    return Outcome(
        kind=OutcomeKind.BLOCKED,
        confidence=OutcomeConfidence.HIGH,
        summary="PR not yet mergeable (CI pending or merge conflict)",
        artifacts=OutcomeArtifacts(pr_number=pr_number),
    )
