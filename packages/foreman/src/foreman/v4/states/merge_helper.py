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
   applicable healer → the classifier (foreman#317, see below).

Classifier (foreman#317)
-------------------------
When no healer applies, the old code fell straight through to a blanket
BLOCKED — which looped forever on a genuinely CI-failed PR (review finding
C1). Instead it now reads ``state.mergeable_state`` and
``ctx.git.required_check_state(...)`` and routes per the spec's routing
table (``docs/superpowers/specs/foreman-issue-317-spec.md``):

- ``mergeable_state == "dirty"`` (textual merge conflict) → NEEDS_FIX,
  ``details={"fix_reason": "merge_conflict"}`` — only a Fixer can resolve
  a real conflict (Decision D).
- ``mergeable_state == "draft"`` → NEEDS_HELP — a foreman-managed impl PR
  should never be a draft; this is the other pre-check-runs
  ``mergeable_state`` special-case (alongside ``dirty``) so it can't fall
  through to the check-state branches below.
- required check ``FAILED`` → NEEDS_FIX, ``details={"fix_reason":
  "ci_failed"}`` — the C1 fix.
- required check ``ACTION_REQUIRED`` → NEEDS_HELP — a human gate ImplFix
  can't act on.
- required check ``TIMED_OUT_OR_CANCELLED`` → ``_rerun_or_escalate``: a
  bounded re-run-once-then-escalate cycle (Decision T — likely an infra
  flake).
- ``PENDING`` or ``PASSED``-but-still-``blocked`` (GitHub hasn't
  recomputed ``mergeable_state`` yet) → BLOCKED — the legitimate wait,
  unchanged from before #317.

``NEEDS_FIX`` reuses the existing ``OutcomeKind`` (already routes
Implementing/ImplReview → ImplFix); no new kind was added.

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

Check-rerun bound (foreman#317)
--------------------------------
A parallel bound protects the ``TIMED_OUT_OR_CANCELLED`` re-run path the
same way: ``_prior_rerun_count`` counts prior BLOCKED rows carrying the
``reran_checks`` marker, and at/after ``MAX_CHECK_RERUNS`` such cycles
``_rerun_or_escalate`` returns NEEDS_HELP instead of issuing another
``rerun_failed_checks`` call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from foreman.v4.git_provider import RequiredCheckState
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


#: Max check-run re-run attempts on one merge state before a still
#: timed-out/cancelled PR escalates to NEEDS_HELP. One is deliberately
#: tight — Decision T (foreman#317 spec): a TIMED_OUT_OR_CANCELLED
#: required check is likely an infra flake rather than a defect ImplFix
#: can act on, so a single re-run absorbs the common case and a human
#: decides if it recurs.
MAX_CHECK_RERUNS = 1


#: Marker key written into a re-run-issued BLOCKED outcome's ``details``
#: (value ``True``). The re-run bound counts ONLY rows carrying this
#: marker — mirrors ``HEAL_ACTION_DETAIL_KEY``'s role in
#: ``_prior_blocked_heal_count``: plain CI-pending BLOCKED rows must not
#: count toward it either.
RERUN_DETAIL_KEY = "reran_checks"


def _prior_rerun_count(ctx: StateContext) -> int:
    """Count prior re-run-issued BLOCKED instances of the current state.

    Mirrors ``_prior_blocked_heal_count`` exactly, keyed on
    ``details.get(RERUN_DETAIL_KEY)`` instead of the heal-action marker —
    a TIMED_OUT_OR_CANCELLED check-run re-run (Decision T) is a parallel
    bounded self-loop to the heal-action bound, journaled the same way, so
    the same read-back logic applies.
    """
    rows = ctx.repo.list_state_instances_for_ticket(ctx.ticket.id)
    count = 0
    for r in rows:
        if r.state_name != ctx.instance.state_name:
            continue
        if r.outcome_kind != OutcomeKind.BLOCKED:
            continue
        details = (r.outcome_payload or {}).get("details") or {}
        if details.get(RERUN_DETAIL_KEY):
            count += 1
    return count


def _rerun_or_escalate(ctx: StateContext, pr_number: int) -> Outcome:
    """Re-run a PR's timed-out/cancelled required checks once, then escalate.

    foreman#317 Decision T: a TIMED_OUT_OR_CANCELLED required check is
    likely an infra flake rather than something ImplFix can act on, so
    this re-runs the checks a single time (bounded by
    ``MAX_CHECK_RERUNS``) before giving up and asking a human.
    """
    prior = _prior_rerun_count(ctx)
    if prior >= MAX_CHECK_RERUNS:
        return Outcome(
            kind=OutcomeKind.NEEDS_HELP,
            confidence=OutcomeConfidence.HIGH,
            summary=(f"checks timed out/cancelled after {MAX_CHECK_RERUNS} re-run — escalating"),
            artifacts=OutcomeArtifacts(pr_number=pr_number),
        )
    # attempt_merge guarantees ctx.git is non-None before any caller
    # reaches here; narrow for the type checker (same pattern as
    # BehindBranchHealer.heal in merge_healers.py).
    assert ctx.git is not None
    ctx.git.rerun_failed_checks(project=ctx.ticket.project, pr_number=pr_number)
    return Outcome(
        kind=OutcomeKind.BLOCKED,
        confidence=OutcomeConfidence.HIGH,
        summary="checks timed out/cancelled — re-running once",
        artifacts=OutcomeArtifacts(pr_number=pr_number),
        details={RERUN_DETAIL_KEY: True},
    )


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

    # foreman#317: no healer applied — classify on ground truth instead of
    # a blanket BLOCKED, which looped forever on CI-failed PRs (review
    # finding C1). "dirty" is a textual merge conflict that only a Fixer
    # can resolve (Decision D — update_branch/rebase just fails on a real
    # conflict); everything else defers to the required check-run state.
    if state.mergeable_state == "dirty":
        return Outcome(
            kind=OutcomeKind.NEEDS_FIX,
            confidence=OutcomeConfidence.HIGH,
            summary="merge conflict with base — routing to ImplFix to resolve",
            artifacts=OutcomeArtifacts(pr_number=pr_number),
            details={"fix_reason": "merge_conflict"},
        )
    # "draft" is the other pre-check-runs mergeable_state (alongside
    # "dirty"): a foreman-managed impl PR should never be a draft, so
    # falling through to the check-state branches below would classify on
    # a signal that doesn't apply yet and could loop BLOCKED on this one
    # state — a human should look, per the spec routing table.
    if state.mergeable_state == "draft":
        return Outcome(
            kind=OutcomeKind.NEEDS_HELP,
            confidence=OutcomeConfidence.HIGH,
            summary="PR is a draft — anomalous for a foreman-managed impl PR; escalating",
            artifacts=OutcomeArtifacts(pr_number=pr_number),
        )
    check = ctx.git.required_check_state(project=ctx.ticket.project, pr_number=pr_number)
    if check == RequiredCheckState.FAILED:
        return Outcome(
            kind=OutcomeKind.NEEDS_FIX,
            confidence=OutcomeConfidence.HIGH,
            summary="required CI check failed — routing to ImplFix",
            artifacts=OutcomeArtifacts(pr_number=pr_number),
            details={"fix_reason": "ci_failed"},
        )
    if check == RequiredCheckState.ACTION_REQUIRED:
        return Outcome(
            kind=OutcomeKind.NEEDS_HELP,
            confidence=OutcomeConfidence.HIGH,
            summary="a required check needs manual action — escalating",
            artifacts=OutcomeArtifacts(pr_number=pr_number),
        )
    if check == RequiredCheckState.TIMED_OUT_OR_CANCELLED:
        return _rerun_or_escalate(ctx, pr_number)
    # PENDING (CI still running) or PASSED-but-blocked (GitHub hasn't
    # recomputed mergeable_state yet, a transient lag) → wait for the next
    # poll — the legitimate case, not a failure.
    return Outcome(
        kind=OutcomeKind.BLOCKED,
        confidence=OutcomeConfidence.HIGH,
        summary="CI still in flight — re-polling",
        artifacts=OutcomeArtifacts(pr_number=pr_number),
    )
