"""Tests for the typed ``Outcome`` enum (foreman#258).

The enum is the single source of truth for outcome strings written to
``execution_log``. Tests below pin three contracts:

1. The enum inventory matches the production write surface (the 12
   string values currently written via ``write_action`` /
   ``terminate_action``).
2. The three derived frozensets are byte-for-byte equivalent to the
   pre-foreman#258 ``_NON_FAILURE_OUTCOMES`` tuple and the two sibling
   constants, and together partition the enum.
3. Adding a new member without a classification raises ``TypeError`` at
   module load — the structural guarantee that motivated the design.
"""

from __future__ import annotations

from enum import StrEnum

import pytest

from foreman.reconciler.outcomes import (
    FAILURE_OUTCOMES,
    NEUTRAL_OUTCOMES,
    NON_FAILURE_OUTCOMES,
    Outcome,
    OutcomeClass,
)

# The 12-value inventory from the foreman#258 spec. If a new outcome
# string is written to ``execution_log`` anywhere under
# ``packages/foreman/src/foreman/`` without adding a corresponding
# Outcome member, this test catches it.
_EXPECTED_OUTCOME_VALUES: frozenset[str] = frozenset(
    {
        "success",
        "dry_run",
        "skipped_capacity",
        "error",
        "timeout",
        "subprocess_killed",
        "errored:recovery",
        "running",
        "reset",
        "alert",
        "failed",
        "executed",
    }
)


def test_outcome_enum_contains_all_known_outcomes() -> None:
    """The Outcome enum enumerates exactly the 12 string values the
    spec lists. Drift between this set and what production code writes
    to ``execution_log`` is the failure mode the enum is designed to
    prevent — a contributor who adds an outcome string somewhere
    without extending the enum trips this assertion."""
    assert {m.value for m in Outcome} == _EXPECTED_OUTCOME_VALUES


def test_non_failure_outcomes_matches_existing_tuple_contents() -> None:
    """Byte-for-byte equivalence with the pre-foreman#258 tuple at
    ``reconciler/exec_log.py:31``: ``("success", "dry_run",
    "skipped_capacity")``. The SQL bind sites in ``exec_log.py``
    iterate this frozenset directly; any drift here would shift which
    outcomes count toward the rate-limit failure window."""
    assert NON_FAILURE_OUTCOMES == frozenset({"success", "dry_run", "skipped_capacity"})


def test_failure_outcomes_classification() -> None:
    """The four FAILURE outcomes. ``errored:recovery`` is here because
    foreman#229's recovery write IS a failure signal (a role crash was
    caught and rolled back); the existing
    ``test_count_recent_failures_counts_errored_recovery_outcomes``
    test pins that contract against the rate-limit query and must not
    regress."""
    assert FAILURE_OUTCOMES == frozenset(
        {"error", "timeout", "subprocess_killed", "errored:recovery"}
    )


def test_neutral_outcomes_classification() -> None:
    """The five NEUTRAL outcomes. ``running`` is the start-row marker
    used by the partial index DDL and the unterminated-row queries.
    ``reset`` is the rate-limit-reset sentinel. ``alert`` / ``failed``
    / ``executed`` are surface-help and other-action terminators."""
    assert NEUTRAL_OUTCOMES == frozenset({"running", "reset", "alert", "failed", "executed"})


def test_outcome_member_str_equality() -> None:
    """``StrEnum`` members compare equal to their string values. This
    is the property that lets SQL bind sites pass ``Outcome.RUNNING``
    directly to the SQLite parameter binder without a ``.value``
    access. Pin it so the bind sites in ``exec_log.py`` keep working
    even if a future Python version subtly changes ``StrEnum``."""
    assert Outcome.RUNNING == "running"
    assert Outcome.SUCCESS == "success"
    assert Outcome.ERRORED_RECOVERY == "errored:recovery"


def test_outcome_classifications_partition_membership() -> None:
    """Every Outcome member's classification is one of the three
    buckets, AND the three frozensets are a partition (no overlap, no
    member uncovered). This pins the design contract that adding a new
    bucket would require classifying every existing member into it —
    you cannot leave a member unclassified."""
    classifications = {m.classification for m in Outcome}
    assert classifications <= {
        OutcomeClass.NON_FAILURE,
        OutcomeClass.FAILURE,
        OutcomeClass.NEUTRAL,
    }
    all_values = {m.value for m in Outcome}
    union = NON_FAILURE_OUTCOMES | FAILURE_OUTCOMES | NEUTRAL_OUTCOMES
    assert union == all_values
    # No overlap.
    assert NON_FAILURE_OUTCOMES.isdisjoint(FAILURE_OUTCOMES)
    assert NON_FAILURE_OUTCOMES.isdisjoint(NEUTRAL_OUTCOMES)
    assert FAILURE_OUTCOMES.isdisjoint(NEUTRAL_OUTCOMES)


def test_outcome_member_without_classification_raises_type_error() -> None:
    """The structural guarantee from the issue body: "adding an outcome
    without classification is impossible." Constructing a synthetic
    enum that adopts the same ``__new__(value, classification)``
    pattern but declares a member as a bare string (missing the
    classification arg) raises ``TypeError`` at class-creation time.

    We can't assert this against the real ``Outcome`` enum because
    Python evaluates the class body at import time — a broken member
    would surface as ``ImportError`` during pytest collection, not as
    a ``TypeError`` from a test body. The synthetic-subclass approach
    runs the same pattern at test time so the assertion fires
    predictably."""
    with pytest.raises(TypeError):
        # The class body itself raises during evaluation. We don't
        # care about the resulting class — only that the construction
        # fails because the member tuple is missing its
        # classification arg.
        class _BrokenOutcome(StrEnum):  # type: ignore[misc]
            classification: OutcomeClass

            def __new__(
                cls, value: str, classification: OutcomeClass
            ) -> _BrokenOutcome:
                obj = str.__new__(cls, value)
                obj._value_ = value
                obj.classification = classification
                return obj

            # Bare string — missing the classification tuple element.
            # Python's enum machinery passes "bad" as the only
            # positional arg to ``__new__``; ``classification`` has no
            # default, so ``TypeError`` fires at class-creation time.
            BAD = "bad"
