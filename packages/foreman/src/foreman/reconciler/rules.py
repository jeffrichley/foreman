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
    return "foreman:hold" in ctx.issue.labels


def _needs_help_label(ctx: ActionContext) -> bool:
    return "foreman:needs-help" in ctx.issue.labels


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
        for label in ("foreman:impl-review", "foreman:impl-approved", "foreman:impl-fix")
    )


def _spec_pr_ci_failure(ctx: ActionContext) -> bool:
    if ctx.pr is None or ctx.pr.ci_status != "FAILURE":
        return False
    # Symmetric to ``_impl_pr_ci_failure``: refuse to fire on an impl-shaped
    # PR even when ``foreman:planning`` is set (e.g., during the brief
    # stacked-PR window where both shapes are linked to the same issue).
    if not ctx.pr.head_ref.startswith("foreman/issue-"):
        return False
    return "foreman:planning" in ctx.issue.labels


_MAX_FIX_ATTEMPTS = 3
_MAX_IMPL_ATTEMPTS = 3


def _fix_attempts_exhausted(ctx: ActionContext) -> bool:
    # Budget is per-impl-cycle: only impl-side Fixer dispatches count toward
    # the attempt cap. Spec-side fixes don't share the same retry budget.
    return (
        "foreman:impl-fix" in ctx.issue.labels
        and ctx.log.count_completed("dispatch_fixer_impl", ctx.ticket_id) >= _MAX_FIX_ATTEMPTS
    )


def _spec_fix_attempts_exhausted(ctx: ActionContext) -> bool:
    # Symmetric spec-side budget: only ``dispatch_fixer_spec`` dispatches count.
    # Pairs with ``_spec_fix_pending`` below — both scope to the spec-side
    # action key so the spec retry budget is independent of impl-side.
    return (
        "foreman:spec-fix" in ctx.issue.labels
        and ctx.log.count_completed("dispatch_fixer_spec", ctx.ticket_id) >= _MAX_FIX_ATTEMPTS
    )


def _impl_attempts_exhausted(ctx: ActionContext) -> bool:
    return (
        "foreman:plan-approved" in ctx.issue.labels
        and ctx.log.count_completed("dispatch_worker", ctx.ticket_id) >= _MAX_IMPL_ATTEMPTS
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
)


def _planning_no_pr(ctx: ActionContext) -> bool:
    return (
        "foreman:planning" in ctx.issue.labels
        and ctx.pr is None
        and not ctx.log.has_unterminated("dispatch_planner", ctx.ticket_id)
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
    return (
        "foreman:planning" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.head_ref.startswith("foreman/issue-")
        and not ctx.log.has_unterminated("dispatch_reviewer_spec", ctx.ticket_id)
    )


def _plan_approved_pr_green_and_flag(ctx: ActionContext) -> bool:
    # Head-ref filter (MEDIUM #11): refuse to merge a non-spec-shaped PR even
    # when the daemon picked one for this ticket (transient stacked-PR
    # window where both spec and impl PRs are linked to the same issue).
    # Without this filter, ``merge_spec_pr`` could call ``host.merge_pr`` on
    # an impl PR while logging the rule name as "merge_spec_pr".
    return (
        "foreman:plan-approved" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.head_ref.startswith("foreman/issue-")
        and ctx.pr.mergeable == "MERGEABLE"
        and ctx.pr.ci_status == "SUCCESS"
        and ctx.auto_merge_spec
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
    if "foreman:planning" not in ctx.issue.labels:
        return False
    if ctx.log.has_recent(
        "advance_label_to_plan_approved", ctx.ticket_id, within_seconds=3600 * 24
    ):
        return False
    return True


def _plan_approved_no_impl_pr(ctx: ActionContext) -> bool:
    return (
        "foreman:plan-approved" in ctx.issue.labels
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
    return (
        "foreman:impl-review" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.head_ref.startswith("foreman/impl-")
        and ctx.pr.ci_status == "SUCCESS"
        and not ctx.log.has_unterminated("dispatch_reviewer_impl", ctx.ticket_id)
    )


def _impl_fix_pending(ctx: ActionContext) -> bool:
    # Impl-side Fixer flow. Pairs with ``_fix_attempts_exhausted`` above —
    # both scope to ``dispatch_fixer_impl`` so a future spec-side Fixer
    # rule (planned for a later stage) gets its own independent budget.
    # Head-ref filter (4c): refuse to dispatch impl-side Fixer onto a
    # spec-shaped PR.
    return (
        "foreman:impl-fix" in ctx.issue.labels
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
        "foreman:spec-fix" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.head_ref.startswith("foreman/issue-")
        and not ctx.log.has_unterminated("dispatch_fixer_spec", ctx.ticket_id)
        and ctx.log.count_completed("dispatch_fixer_spec", ctx.ticket_id) < _MAX_FIX_ATTEMPTS
    )


def _impl_approved_pr_green_and_flag(ctx: ActionContext) -> bool:
    # Head-ref filter (MEDIUM #11): symmetric to ``_plan_approved_pr_green_and_flag``.
    # Refuse to fire on a spec-shaped PR even when the daemon picked one for
    # this ticket — ``merge_impl_pr`` must only merge impl-shaped branches.
    return (
        "foreman:impl-approved" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.head_ref.startswith("foreman/impl-")
        and ctx.pr.mergeable == "MERGEABLE"
        and ctx.pr.ci_status == "SUCCESS"
        and ctx.auto_merge_impl
    )


def _impl_pr_merged_label_lagging(ctx: ActionContext) -> bool:
    if ctx.pr is None or not ctx.pr.is_merged:
        return False
    # Head-ref filter (MEDIUM #11): the lagging-label rule must refuse to
    # advance ``impl-approved → done`` when ``ctx.pr`` is a spec-shaped PR.
    if not ctx.pr.head_ref.startswith("foreman/impl-"):
        return False
    if "foreman:impl-approved" not in ctx.issue.labels:
        return False
    if ctx.log.has_recent(
        "advance_label_to_done", ctx.ticket_id, within_seconds=3600 * 24
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
        name="merge_spec_pr",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=115,
        when=_plan_approved_pr_green_and_flag,
        then=Action.MERGE_SPEC_PR,
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
        name="merge_impl_pr",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=160,
        when=_impl_approved_pr_green_and_flag,
        then=Action.MERGE_IMPL_PR,
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


def evaluate(ctx: ActionContext, *, rules: tuple[Rule, ...] | None = None) -> Action:
    """Run the catalog over the context. Returns the first matching rule's
    action, or Action.NOOP if no rule matches.

    A predicate that raises is treated as "did not match" — the evaluator
    logs the exception and continues. Rationale: a broken predicate must not
    halt the reconciler; rules are independent.
    """
    catalog = RULES if rules is None else rules
    for rule in catalog:
        try:
            if rule.when(ctx):
                return rule.then
        except Exception:
            logger.exception(
                "rule %r raised during evaluation for ticket %s; treating as no-match",
                rule.name,
                ctx.ticket_id,
            )
            continue
    return Action.NOOP
