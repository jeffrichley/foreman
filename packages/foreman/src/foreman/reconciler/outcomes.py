"""Typed catalog of every outcome string written to the ``execution_log`` table.

foreman#258 design ask (Jeff, 2026-06-10 PM): "Can we make it so you only
add to one place? If not, and it can be forgotten, that is an issue."

The :class:`Outcome` enum is the single source of truth for outcome
strings. Each member carries its own ``classification`` (NON_FAILURE /
FAILURE / NEUTRAL) at the point of definition; a contributor who adds a
new member as a bare string (``BAD = "bad"`` instead of
``BAD = ("bad", OutcomeClass.NEUTRAL)``) trips a ``TypeError`` at module
load — Python's enum machinery raises before any test gets a chance to
run. The "forgot to update _NON_FAILURE_OUTCOMES" failure mode that bit
us on foreman#227 is now structurally impossible.

The three derived frozensets (:data:`NON_FAILURE_OUTCOMES`,
:data:`FAILURE_OUTCOMES`, :data:`NEUTRAL_OUTCOMES`) are computed at
module load by filtering the enum on the classification attribute.
SQL bind sites in ``reconciler.exec_log`` iterate the frozensets
directly; the placeholder shape (``",".join("?" for _ in ...)``) and
parameter splat (``*NON_FAILURE_OUTCOMES``) work because ``frozenset``
is iterable in both contexts.

Adding a new outcome:

1. Add one new line to :class:`Outcome`: ``NEW = ("new", OutcomeClass.<bucket>)``.
2. That's it. Both the enum membership AND the derived frozenset are
   updated; no separate constant to keep in sync.
"""

from __future__ import annotations

from enum import Enum, StrEnum


class OutcomeClass(Enum):
    """The three classification buckets a :class:`Outcome` can belong to.

    NON_FAILURE: ``success`` and administrative outcomes that represent
    no real attempt and never count toward the rate-limit failure
    window. ``success`` clears the consecutive-failure counter.

    FAILURE: outcomes that count toward the rate-limit. ``errored:recovery``
    sits here because foreman#229's recovery write IS a failure
    signal — it indicates a role crash was caught and rolled back
    (foreman#258 reference test pins this contract).

    NEUTRAL: start markers and sentinels that are filtered out of
    terminator-counting queries. ``running`` is the start-row marker;
    ``reset`` is the rate-limit reset sentinel; ``alert`` / ``failed`` /
    ``executed`` are surface-help / advance-label-to-failed /
    other-action terminators that report the outcome of the rule
    action itself rather than a dispatched role.
    """

    NON_FAILURE = "non_failure"
    FAILURE = "failure"
    NEUTRAL = "neutral"


class Outcome(StrEnum):
    """Every outcome string written to the ``execution_log`` table.

    The enum is a ``StrEnum`` so members compare equal to their string
    values — SQLite parameter binding receives the value verbatim, and
    f-string interpolation produces the bare string. Use ``.value``
    explicitly when interpolating into SQL DDL (where the result is
    parsed by SQLite without going through the binder) so the format
    cannot accidentally produce ``"Outcome.RUNNING"`` instead of
    ``"running"``.

    Each member's right-hand side is a tuple of
    ``(value: str, classification: OutcomeClass)``. The custom
    :meth:`__new__` requires the classification — omitting it raises
    a ``TypeError`` at class-creation time, before module load
    completes.

    Adding a new outcome is one line below. Forgetting the
    classification is impossible.
    """

    classification: OutcomeClass

    def __new__(cls, value: str, classification: OutcomeClass) -> Outcome:
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.classification = classification
        return obj

    SUCCESS = ("success", OutcomeClass.NON_FAILURE)
    DRY_RUN = ("dry_run", OutcomeClass.NON_FAILURE)
    SKIPPED_CAPACITY = ("skipped_capacity", OutcomeClass.NON_FAILURE)
    ERROR = ("error", OutcomeClass.FAILURE)
    TIMEOUT = ("timeout", OutcomeClass.FAILURE)
    SUBPROCESS_KILLED = ("subprocess_killed", OutcomeClass.FAILURE)
    ERRORED_RECOVERY = ("errored:recovery", OutcomeClass.FAILURE)
    RUNNING = ("running", OutcomeClass.NEUTRAL)
    RESET = ("reset", OutcomeClass.NEUTRAL)
    ALERT = ("alert", OutcomeClass.NEUTRAL)
    FAILED = ("failed", OutcomeClass.NEUTRAL)
    EXECUTED = ("executed", OutcomeClass.NEUTRAL)


NON_FAILURE_OUTCOMES: frozenset[str] = frozenset(
    m.value for m in Outcome if m.classification is OutcomeClass.NON_FAILURE
)
"""String values whose classification is :attr:`OutcomeClass.NON_FAILURE`.

Byte-for-byte equivalent to the pre-foreman#258
``_NON_FAILURE_OUTCOMES`` tuple. SQL bind sites in
``reconciler.exec_log`` iterate this directly.
"""

FAILURE_OUTCOMES: frozenset[str] = frozenset(
    m.value for m in Outcome if m.classification is OutcomeClass.FAILURE
)
"""String values whose classification is :attr:`OutcomeClass.FAILURE`."""

NEUTRAL_OUTCOMES: frozenset[str] = frozenset(
    m.value for m in Outcome if m.classification is OutcomeClass.NEUTRAL
)
"""String values whose classification is :attr:`OutcomeClass.NEUTRAL`."""


__all__ = [
    "FAILURE_OUTCOMES",
    "NEUTRAL_OUTCOMES",
    "NON_FAILURE_OUTCOMES",
    "Outcome",
    "OutcomeClass",
]
