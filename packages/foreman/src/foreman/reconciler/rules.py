"""Rule catalog + evaluator for v3.

Rules are pure predicates over ActionContext. The evaluator scans RULES in
ascending precedence order; the first matching rule's action fires. A safety
rule that fires preempts every forward-progress rule below it.

The RULES list is appended to in Tasks 5 (safety) and 6 (forward-progress).
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Callable

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


def _needs_help_label(ctx: ActionContext) -> bool:
    return "foreman:needs-help" in ctx.issue.labels


def _mergeable_conflict(ctx: ActionContext) -> bool:
    return ctx.pr is not None and ctx.pr.mergeable == "CONFLICTING"


def _impl_pr_ci_failure(ctx: ActionContext) -> bool:
    if ctx.pr is None or ctx.pr.ci_status != "FAILURE":
        return False
    return any(
        label in ctx.issue.labels
        for label in ("foreman:impl-review", "foreman:impl-approved", "foreman:impl-fix")
    )


def _spec_pr_ci_failure(ctx: ActionContext) -> bool:
    if ctx.pr is None or ctx.pr.ci_status != "FAILURE":
        return False
    return "foreman:planning" in ctx.issue.labels


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
)


RULES: tuple[Rule, ...] = _SAFETY_RULES  # forward-progress rules append in Task 6


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
