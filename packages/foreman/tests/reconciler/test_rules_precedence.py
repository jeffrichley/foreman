"""Invariant test: every safety rule's precedence is strictly less than every
forward-progress rule's. Catalog mutations that violate this break the build.
"""

from __future__ import annotations

from foreman.reconciler.rules import RULES, PrecedenceTier


def test_rules_have_unique_precedence_values() -> None:
    precedences = [rule.precedence for rule in RULES]
    assert len(precedences) == len(set(precedences)), (
        "Two rules share the same precedence; ordering is ambiguous"
    )


def test_safety_tier_uses_precedence_below_100() -> None:
    for rule in RULES:
        if rule.tier is PrecedenceTier.SAFETY:
            assert rule.precedence < 100, (
                f"Safety rule {rule.name!r} has precedence {rule.precedence}; must be < 100"
            )


def test_forward_progress_tier_uses_precedence_at_or_above_100() -> None:
    for rule in RULES:
        if rule.tier is PrecedenceTier.FORWARD_PROGRESS:
            assert rule.precedence >= 100, (
                f"Forward-progress rule {rule.name!r} has precedence "
                f"{rule.precedence}; must be >= 100"
            )


def test_safety_rules_all_precede_forward_progress_rules() -> None:
    safety_max = max(
        (r.precedence for r in RULES if r.tier is PrecedenceTier.SAFETY),
        default=-1,
    )
    progress_min = min(
        (r.precedence for r in RULES if r.tier is PrecedenceTier.FORWARD_PROGRESS),
        default=10**9,
    )
    assert safety_max < progress_min, (
        f"Safety precedence max ({safety_max}) must be < forward-progress min ({progress_min})"
    )


def test_rules_are_sorted_by_precedence() -> None:
    precedences = [rule.precedence for rule in RULES]
    assert precedences == sorted(precedences), (
        "RULES list must be in ascending precedence order; first match wins"
    )
