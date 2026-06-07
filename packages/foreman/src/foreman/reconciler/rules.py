"""Rule catalog + evaluator for v3.

Rules are pure predicates over ActionContext. The evaluator scans RULES in
ascending precedence order; the first matching rule's action fires. A safety
rule that fires preempts every forward-progress rule below it.

The RULES list is appended to in Tasks 5 (safety) and 6 (forward-progress).
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable
from dataclasses import dataclass

from foreman.labels import Labels
from foreman.reconciler.actions import Action, ActionContext

logger = logging.getLogger(__name__)


class PrecedenceTier(enum.Enum):
    """Two-tier ordering: safety always preempts forward-progress."""

    SAFETY = "safety"
    FORWARD_PROGRESS = "forward_progress"


@dataclass(frozen=True)
class Rule:
    """One rule. `when` is the predicate; `then` is the action to emit on True."""

    name: str
    tier: PrecedenceTier
    precedence: int
    when: Callable[[ActionContext], bool]
    then: Action


def _hold_label(ctx: ActionContext) -> bool:
    return Labels.HOLD in ctx.issue.labels


def _needs_help_label(ctx: ActionContext) -> bool:
    return Labels.NEEDS_HELP in ctx.issue.labels


def _mergeable_conflict(ctx: ActionContext) -> bool:
    return ctx.pr is not None and ctx.pr.mergeable == "CONFLICTING"


def _impl_pr_ci_failure(ctx: ActionContext) -> bool:
    if ctx.pr is None or ctx.pr.ci_status != "FAILURE":
        return False
    # Head-ref filter: a FAILURE on a spec-shaped PR must not trigger the
    # impl-side safety rule even if a stale impl-side label lingers on the
    # issue (adversarial review MEDIUM #4c). Filter by branch shape, not
    # label alone, so misaligned PR/label combinations are inert.
    if not ctx.pr.head_ref.startswith("foreman/impl-"):
        return False
    return any(
        label in ctx.issue.labels
        for label in (Labels.IMPL_REVIEW, Labels.IMPL_APPROVED, Labels.IMPL_FIX)
    )


def _spec_pr_ci_failure(ctx: ActionContext) -> bool:
    if ctx.pr is None or ctx.pr.ci_status != "FAILURE":
        return False
    # Symmetric to ``_impl_pr_ci_failure``: refuse to fire on an impl-shaped
    # PR even when ``foreman:planning`` is set (e.g., during the brief
    # stacked-PR window where both shapes are linked to the same issue).
    if not ctx.pr.head_ref.startswith("foreman/issue-"):
        return False
    return Labels.PLANNING in ctx.issue.labels


_MAX_FIX_ATTEMPTS = 3
_MAX_IMPL_ATTEMPTS = 3
_MAX_REVIEWER_ATTEMPTS = 3


def _fix_attempts_exhausted(ctx: ActionContext) -> bool:
    # Budget is per-impl-cycle: only impl-side Fixer dispatches count toward
    # the attempt cap. Spec-side fixes don't share the same retry budget.
    return (
        Labels.IMPL_FIX in ctx.issue.labels
        and ctx.log.count_completed("dispatch_fixer_impl", ctx.ticket_id) >= _MAX_FIX_ATTEMPTS
    )


def _spec_fix_attempts_exhausted(ctx: ActionContext) -> bool:
    # Symmetric spec-side budget: only ``dispatch_fixer_spec`` dispatches count.
    # Pairs with ``_spec_fix_pending`` below — both scope to the spec-side
    # action key so the spec retry budget is independent of impl-side.
    return (
        Labels.SPEC_FIX in ctx.issue.labels
        and ctx.log.count_completed("dispatch_fixer_spec", ctx.ticket_id) >= _MAX_FIX_ATTEMPTS
    )


def _impl_attempts_exhausted(ctx: ActionContext) -> bool:
    return (
        Labels.PLAN_APPROVED in ctx.issue.labels
        and ctx.log.count_completed("dispatch_worker", ctx.ticket_id) >= _MAX_IMPL_ATTEMPTS
    )


def _reviewer_spec_attempts_exhausted(ctx: ActionContext) -> bool:
    # Spec-side Reviewer budget. Pairs with the forward-progress gate in
    # ``_planning_pr_needs_review``. After Plan B Stage 1+2 fixed the
    # crashed-Reviewer silent-stall by terminating the running log row
    # promptly, the next tick re-fires the rule. If the root cause persists
    # (e.g., LLM provider down), the loop becomes infinite hot-spawn — every
    # other role has a budget cap, Reviewer was the only one without. This
    # rule + the count gate in ``_planning_pr_needs_review`` close that gap.
    # All terminations burn budget (matches Worker + Fixer behavior); the
    # action key is scoped to ``dispatch_reviewer_spec`` so the spec-side
    # budget is independent of impl-side.
    return (
        Labels.PLANNING in ctx.issue.labels
        and ctx.log.count_completed("dispatch_reviewer_spec", ctx.ticket_id) >= _MAX_REVIEWER_ATTEMPTS
    )


def _reviewer_impl_attempts_exhausted(ctx: ActionContext) -> bool:
    # Impl-side Reviewer budget. Symmetric to ``_reviewer_spec_attempts_exhausted``
    # but scoped to ``dispatch_reviewer_impl`` + ``foreman:impl-review`` label.
    return (
        Labels.IMPL_REVIEW in ctx.issue.labels
        and ctx.log.count_completed("dispatch_reviewer_impl", ctx.ticket_id) >= _MAX_REVIEWER_ATTEMPTS
    )


def _safety_with_rate_limit(predicate):
    """Wrap a safety predicate so it stops re-firing if surface_help has been
    emitted for this ticket in the last hour.
    """
    def wrapped(ctx: ActionContext) -> bool:
        if not predicate(ctx):
            return False
        if ctx.log.has_recent("surface_help", ctx.ticket_id, within_seconds=3600):
            return False
        return True
    return wrapped


_SAFETY_RULES: tuple[Rule, ...] = (
    Rule(
        name="hold_label_blocks",
        tier=PrecedenceTier.SAFETY,
        precedence=5,
        when=_hold_label,
        then=Action.NOOP,
    ),
    Rule(
        name="needs_help_label",
        tier=PrecedenceTier.SAFETY,
        precedence=10,
        when=_safety_with_rate_limit(_needs_help_label),
        then=Action.SURFACE_HELP,
    ),
    Rule(
        name="mergeable_conflict",
        tier=PrecedenceTier.SAFETY,
        precedence=20,
        when=_safety_with_rate_limit(_mergeable_conflict),
        then=Action.SURFACE_HELP,
    ),
    Rule(
        name="impl_pr_ci_failure",
        tier=PrecedenceTier.SAFETY,
        precedence=30,
        when=_safety_with_rate_limit(_impl_pr_ci_failure),
        then=Action.SURFACE_HELP,
    ),
    Rule(
        name="spec_pr_ci_failure",
        tier=PrecedenceTier.SAFETY,
        precedence=40,
        when=_safety_with_rate_limit(_spec_pr_ci_failure),
        then=Action.SURFACE_HELP,
    ),
    Rule(
        name="fix_attempts_exhausted",
        tier=PrecedenceTier.SAFETY,
        precedence=50,
        when=_safety_with_rate_limit(_fix_attempts_exhausted),
        then=Action.SURFACE_HELP,
    ),
    Rule(
        name="spec_fix_attempts_exhausted",
        tier=PrecedenceTier.SAFETY,
        precedence=55,
        when=_safety_with_rate_limit(_spec_fix_attempts_exhausted),
        then=Action.SURFACE_HELP,
    ),
    Rule(
        name="impl_attempts_exhausted",
        tier=PrecedenceTier.SAFETY,
        precedence=60,
        when=_safety_with_rate_limit(_impl_attempts_exhausted),
        then=Action.SURFACE_HELP,
    ),
    Rule(
        name="reviewer_spec_attempts_exhausted",
        tier=PrecedenceTier.SAFETY,
        precedence=65,
        when=_safety_with_rate_limit(_reviewer_spec_attempts_exhausted),
        then=Action.SURFACE_HELP,
    ),
    Rule(
        name="reviewer_impl_attempts_exhausted",
        tier=PrecedenceTier.SAFETY,
        precedence=70,
        when=_safety_with_rate_limit(_reviewer_impl_attempts_exhausted),
        then=Action.SURFACE_HELP,
    ),
)


def _planning_no_pr(ctx: ActionContext) -> bool:
    # ``count_completed(..., outcome="success") == 0`` gate: if a human closes
    # the spec PR without merging, ``ctx.pr`` flips back to None while the
    # issue still carries ``foreman:planning``. Without the count gate, the
    # rule would re-fire dispatch_planner — spawning a second Planner
    # subprocess that opens another spec PR for the same ticket. One Planner
    # run per ticket is the contract; subsequent attempts must surface to
    # a human (eventually via the existing safety-rate-limit path).
    #
    # We filter on ``outcome="success"`` (rather than counting all
    # terminations) because failed Planner runs — ``error`` from an executor
    # exception, ``timeout`` from the host's tracker, ``errored:recovery``
    # from a daemon-restart sweep — did NOT open a spec PR. Counting them
    # toward the gate would permanently block legitimate re-fire after a
    # transient crash, silently dead-locking the ticket with no escalation.
    # Budget gates (max-N attempts) intentionally use the all-terminations
    # default; idempotence gates like this one need success-only.
    return (
        Labels.PLANNING in ctx.issue.labels
        and ctx.pr is None
        and not ctx.log.has_unterminated("dispatch_planner", ctx.ticket_id)
        and ctx.log.count_completed(
            "dispatch_planner", ctx.ticket_id, outcome="success"
        ) == 0
    )


def _planning_pr_needs_review(ctx: ActionContext) -> bool:
    # Spec-side idempotence gate: only check for an in-flight spec-side
    # Reviewer dispatch. The earlier version also required
    # ``count_completed(...) == 0`` — but after a Fixer-spec cycle moves
    # the label spec-fix → planning, the Reviewer must re-fire on the
    # updated spec PR. A permanent count==0 gate deadlocked that flow
    # (adversarial review HIGH #6). The label-state machine + the
    # has_unterminated check are the right gates:
    #   - while Reviewer runs: has_unterminated=True blocks re-fire
    #   - on success: Reviewer transitions label off planning → predicate False
    #   - on needs_fix: label becomes spec-fix → predicate False
    #   - after Fixer moves spec-fix back to planning → predicate True → re-fire
    # The head-ref filter (4c) ensures we only target spec-shaped PRs;
    # impl-shaped PRs linked to the same issue (brief stacked window)
    # must not trigger spec-side Reviewer.
    # Budget cap (count_completed < _MAX_REVIEWER_ATTEMPTS): pairs with
    # ``_reviewer_spec_attempts_exhausted`` safety rule. Plan B's Stage 1+2
    # fixes terminate the running row on crash so the next tick re-fires;
    # without this cap, a persistent root cause (LLM provider down) creates
    # an infinite hot-spawn loop. Every other dispatch role has a budget;
    # Reviewer was the only gap.
    return (
        Labels.PLANNING in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.head_ref.startswith("foreman/issue-")
        and not ctx.log.has_unterminated("dispatch_reviewer_spec", ctx.ticket_id)
        and ctx.log.count_completed("dispatch_reviewer_spec", ctx.ticket_id) < _MAX_REVIEWER_ATTEMPTS
    )


def _advance_label_to_merging_plan_when_eligible(ctx: ActionContext) -> bool:
    # foreman#165: replaces ``_plan_approved_pr_green_and_flag``. Drives the
    # one-shot label transition from ``plan-approved`` into
    # ``plan-approved + merging-plan``; the subsequent ticks then evaluate
    # the ``attempt_merge_plan`` rule until the PR merges. The
    # ``merging-*`` exclusion stops the label-advance rule from re-firing
    # once the label has already been added.
    # Head-ref filter (MEDIUM #11, preserved from the removed
    # ``_plan_approved_pr_green_and_flag`` predicate): refuse to operate on
    # a non-spec-shaped PR even when the daemon picked one for this ticket
    # (transient stacked-PR window where both spec and impl PRs are linked
    # to the same issue). Without this filter, the label-advance could fire
    # against an impl PR, parking the wrong attempt-merge rule on next tick.
    return (
        Labels.PLAN_APPROVED in ctx.issue.labels
        and Labels.MERGING_PLAN not in ctx.issue.labels
        and Labels.MERGING_IMPL not in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.head_ref.startswith("foreman/issue-")
        and ctx.auto_merge_spec
    )


def _attempt_merge_plan_eligible(ctx: ActionContext) -> bool:
    # foreman#165 (new rule): once ``merging-plan`` is set, the rule fires
    # on every tick until the PR merges. The handler reads live
    # ``mergeStateStatus`` and branches: merge / rebase / wait / needs-help.
    # Re-fire is required (wait / rebase states are no-ops) so there is
    # no ``count_completed`` gate here; idempotence comes from
    # ``ctx.pr.is_merged`` flipping True once the merge lands.
    # Head-ref filter (MEDIUM #11, preserved from the removed
    # ``_plan_approved_pr_green_and_flag`` predicate): only spec-shape
    # branches drive spec-side merge attempts.
    return (
        Labels.MERGING_PLAN in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.head_ref.startswith("foreman/issue-")
    )


def _spec_pr_merged_label_lagging(ctx: ActionContext) -> bool:
    if ctx.pr is None or not ctx.pr.is_merged:
        return False
    # Head-ref filter (MEDIUM #11): the lagging-label rule must refuse to
    # advance ``planning → plan-approved`` when ``ctx.pr`` is an impl-shaped
    # PR even if the spec PR with the same number happened to be merged
    # earlier — only spec-shape merges should drive this transition.
    if not ctx.pr.head_ref.startswith("foreman/issue-"):
        return False
    if Labels.PLANNING not in ctx.issue.labels:
        return False
    if ctx.log.has_recent(
        "advance_label_to_plan_approved", ctx.ticket_id, within_seconds=3600 * 24
    ):
        return False
    return True


def _plan_approved_no_impl_pr(ctx: ActionContext) -> bool:
    return (
        Labels.PLAN_APPROVED in ctx.issue.labels
        and not ctx.log.has_unterminated("dispatch_worker", ctx.ticket_id)
        and ctx.log.count_completed("dispatch_worker", ctx.ticket_id) < _MAX_IMPL_ATTEMPTS
    )


def _impl_review_green(ctx: ActionContext) -> bool:
    # Impl-side idempotence: only check for in-flight impl-side reviewer
    # dispatches. A live spec-side dispatch for the same ticket must not
    # block the impl-side rule (the two operate on different PR shapes).
    # Head-ref filter (4c): only target impl-shaped PRs so a spec PR
    # still linked to this ticket during the stacked window cannot trigger
    # the impl-side Reviewer.
    # Budget cap: symmetric to spec-side; closes the hot-spawn-loop gap
    # Plan B Stage 1+2 introduced by terminating crashed Reviewer rows.
    return (
        Labels.IMPL_REVIEW in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.head_ref.startswith("foreman/impl-")
        and ctx.pr.ci_status == "SUCCESS"
        and not ctx.log.has_unterminated("dispatch_reviewer_impl", ctx.ticket_id)
        and ctx.log.count_completed("dispatch_reviewer_impl", ctx.ticket_id) < _MAX_REVIEWER_ATTEMPTS
    )


def _impl_fix_pending(ctx: ActionContext) -> bool:
    # Impl-side Fixer flow. Pairs with ``_fix_attempts_exhausted`` above —
    # both scope to ``dispatch_fixer_impl`` so a future spec-side Fixer
    # rule (planned for a later stage) gets its own independent budget.
    # Head-ref filter (4c): refuse to dispatch impl-side Fixer onto a
    # spec-shaped PR.
    return (
        Labels.IMPL_FIX in ctx.issue.labels
        and ctx.pr is not None
        and ctx.pr.head_ref.startswith("foreman/impl-")
        and not ctx.log.has_unterminated("dispatch_fixer_impl", ctx.ticket_id)
        and ctx.log.count_completed("dispatch_fixer_impl", ctx.ticket_id) < _MAX_FIX_ATTEMPTS
    )


def _spec_fix_pending(ctx: ActionContext) -> bool:
    # Spec-side Fixer flow. Symmetric to ``_impl_fix_pending`` above but scoped
    # to the spec-side label + action key. Reviewer writes ``foreman:spec-fix``
    # when a spec PR is rejected; this rule consumes it. Without this rule the
    # label would be observed but no forward-progress action would fire — the
    # spec-side fix loop would be dead (adversarial review CRITICAL #3).
    # Head-ref filter (4c): only target spec-shaped PRs.
    return (
        Labels.SPEC_FIX in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.head_ref.startswith("foreman/issue-")
        and not ctx.log.has_unterminated("dispatch_fixer_spec", ctx.ticket_id)
        and ctx.log.count_completed("dispatch_fixer_spec", ctx.ticket_id) < _MAX_FIX_ATTEMPTS
    )


def _advance_label_to_merging_impl_when_eligible(ctx: ActionContext) -> bool:
    # foreman#165: symmetric to ``_advance_label_to_merging_plan_when_eligible``.
    # Once Reviewer's atomic ``set_labels`` lands ``foreman:impl-approved``
    # on the issue, this rule drives the one-shot label transition into
    # ``impl-approved + merging-impl``; the subsequent ticks evaluate the
    # ``attempt_merge_impl`` rule until the PR merges. The ``merging-*``
    # exclusion stops the label-advance from re-firing once the label
    # has already been added.
    # Head-ref filter (MEDIUM #11, preserved from the removed
    # ``_impl_approved_pr_green_and_flag`` predicate): refuse to fire on a
    # spec-shaped PR even when the daemon picked one for this ticket —
    # ``ATTEMPT_MERGE_IMPL`` must only merge impl-shaped branches.
    return (
        Labels.IMPL_APPROVED in ctx.issue.labels
        and Labels.MERGING_PLAN not in ctx.issue.labels
        and Labels.MERGING_IMPL not in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.head_ref.startswith("foreman/impl-")
        and ctx.auto_merge_impl
    )


def _attempt_merge_impl_eligible(ctx: ActionContext) -> bool:
    # foreman#165 (new rule): symmetric to ``_attempt_merge_plan_eligible``.
    # Fires on every tick once ``merging-impl`` is set, until the PR
    # merges (then ``ctx.pr.is_merged`` flips True and the predicate
    # returns False). No ``count_completed`` gate — wait / rebase states
    # are no-ops by design and must be re-evaluable.
    # Head-ref filter (MEDIUM #11): only impl-shape branches drive
    # impl-side merge attempts.
    return (
        Labels.MERGING_IMPL in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.head_ref.startswith("foreman/impl-")
    )


def _impl_pr_merged_label_lagging(ctx: ActionContext) -> bool:
    if ctx.pr is None or not ctx.pr.is_merged:
        return False
    # Head-ref filter (MEDIUM #11): the lagging-label rule must refuse to
    # advance ``impl-approved → done`` when ``ctx.pr`` is a spec-shaped PR.
    if not ctx.pr.head_ref.startswith("foreman/impl-"):
        return False
    if Labels.IMPL_APPROVED not in ctx.issue.labels:
        return False
    if ctx.log.has_recent(
        "advance_label_to_done", ctx.ticket_id, within_seconds=3600 * 24
    ):
        return False
    return True


def _plan_label_only(ctx: ActionContext) -> bool:
    """foreman#171: fire ``advance_label_to_planning`` iff a ticket carries
    ``foreman:plan`` AND nothing else that signals it has already been
    worked.

    The exclusion set has three layers:

    1. **Phase labels** (per spec): any ``foreman:{planning,plan-approved,
       merging-*,spec-fix,impl-*,done,failed}`` means the ticket is past
       the queue phase. Even if someone left ``foreman:plan`` on the
       issue, we don't reset it.

    2. **Operator-control labels**: ``foreman:hold`` and
       ``foreman:needs-help``. Safety-tier rules at higher precedence
       should preempt, but belt-and-suspenders here means we don't
       silently regress if the precedence ordering changes.

    3. **Attempt-counter labels**: ``foreman:impl-attempt-N`` or
       ``foreman:fix-attempt-N`` on the issue means the ticket has been
       through a role cycle. Stale counters survive daemon DB wipes
       (proven 2026-06-06 when ``foreman_*`` volumes were rebuilt and
       the GH labels persisted while ``count_completed`` reset). If the
       counters are present, refuse to advance — an operator should
       clean them up explicitly.
    """
    labels = set(ctx.issue.labels)
    if Labels.PLAN not in labels:
        return False
    _PHASE_EXCLUDED = {
        Labels.PLANNING,
        Labels.PLAN_APPROVED,
        Labels.MERGING_PLAN,
        Labels.MERGING_IMPL,
        Labels.SPEC_FIX,
        Labels.IMPL_REVIEW,
        Labels.IMPL_APPROVED,
        Labels.IMPL_FIX,
        Labels.DONE,
        Labels.FAILED,
        Labels.HOLD,
        Labels.NEEDS_HELP,
    }
    if labels & _PHASE_EXCLUDED:
        return False
    if any(
        lbl.startswith(Labels.IMPL_ATTEMPT_PREFIX)
        or lbl.startswith(Labels.FIX_ATTEMPT_PREFIX)
        for lbl in labels
    ):
        return False
    return True


_PROGRESS_RULES: tuple[Rule, ...] = (
    Rule(
        name="dispatch_planner",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=100,
        when=_planning_no_pr,
        then=Action.DISPATCH_PLANNER,
    ),
    Rule(
        name="dispatch_reviewer_spec",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=105,
        when=_planning_pr_needs_review,
        then=Action.DISPATCH_REVIEWER_SPEC,
    ),
    Rule(
        name="advance_label_to_planning",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        # Precedence 110: must be >= 100 per the FORWARD_PROGRESS tier
        # convention (test_forward_progress_tier_uses_precedence_at_or_above_100).
        # Position relative to dispatch_planner (100) and
        # dispatch_reviewer_spec (105) doesn't matter for correctness —
        # both have predicates that require ``foreman:planning`` (which
        # the ``foreman:plan``-only ticket lacks), so neither matches and
        # the evaluator continues to this rule.
        precedence=110,
        when=_plan_label_only,
        then=Action.ADVANCE_LABEL_TO_PLANNING,
    ),
    Rule(
        name="advance_label_to_merging_plan",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=115,
        when=_advance_label_to_merging_plan_when_eligible,
        then=Action.ADVANCE_LABEL_TO_MERGING_PLAN,
    ),
    Rule(
        name="attempt_merge_plan",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=118,
        when=_attempt_merge_plan_eligible,
        then=Action.ATTEMPT_MERGE_PLAN,
    ),
    Rule(
        name="advance_label_to_plan_approved_lagging",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=120,
        when=_spec_pr_merged_label_lagging,
        then=Action.ADVANCE_LABEL_TO_PLAN_APPROVED,
    ),
    Rule(
        name="dispatch_worker",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=130,
        when=_plan_approved_no_impl_pr,
        then=Action.DISPATCH_WORKER,
    ),
    Rule(
        name="dispatch_reviewer_impl",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=140,
        when=_impl_review_green,
        then=Action.DISPATCH_REVIEWER_IMPL,
    ),
    Rule(
        name="dispatch_fixer_spec",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=145,
        when=_spec_fix_pending,
        then=Action.DISPATCH_FIXER_SPEC,
    ),
    Rule(
        name="dispatch_fixer_impl",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=150,
        when=_impl_fix_pending,
        then=Action.DISPATCH_FIXER_IMPL,
    ),
    Rule(
        name="advance_label_to_merging_impl",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=158,
        when=_advance_label_to_merging_impl_when_eligible,
        then=Action.ADVANCE_LABEL_TO_MERGING_IMPL,
    ),
    Rule(
        name="attempt_merge_impl",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=162,
        when=_attempt_merge_impl_eligible,
        then=Action.ATTEMPT_MERGE_IMPL,
    ),
    Rule(
        name="advance_label_to_done",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=170,
        when=_impl_pr_merged_label_lagging,
        then=Action.ADVANCE_LABEL_TO_DONE,
    ),
)


RULES = _SAFETY_RULES + _PROGRESS_RULES


def evaluate_with_rule(
    ctx: ActionContext, *, rules: tuple[Rule, ...] | None = None
) -> tuple[Action, str | None]:
    """Run the catalog over the context. Returns ``(action, rule_name)`` for
    the first matching rule, or ``(Action.NOOP, None)`` if no rule matches.

    This is the preferred entry point for the daemon: by returning the
    matching rule's name alongside the action, callers avoid a second walk
    of ``RULES`` to recover the attribution (each predicate may hit SQLite
    via ``count_completed`` / ``has_unterminated`` / ``has_recent``, so
    re-evaluating doubles that cost).

    A predicate that raises is treated as "did not match" — the evaluator
    logs the exception and continues. Rationale: a broken predicate must not
    halt the reconciler; rules are independent.
    """
    catalog = RULES if rules is None else rules
    for rule in catalog:
        try:
            if rule.when(ctx):
                return rule.then, rule.name
        except Exception:
            logger.exception(
                "rule %r raised during evaluation for ticket %s; treating as no-match",
                rule.name,
                ctx.ticket_id,
            )
            continue
    return Action.NOOP, None


def evaluate(ctx: ActionContext, *, rules: tuple[Rule, ...] | None = None) -> Action:
    """Thin wrapper over ``evaluate_with_rule`` that drops the rule name.

    Kept for backwards-compatibility with tests + callers that only care
    about the resulting action.  Production code (the reconciler daemon)
    should call ``evaluate_with_rule`` directly to avoid losing the
    attribution.
    """
    action, _ = evaluate_with_rule(ctx, rules=rules)
    return action
