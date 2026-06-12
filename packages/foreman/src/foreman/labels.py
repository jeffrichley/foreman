"""Typed catalog of every ``foreman:*`` label written to GitHub.

D1 (architecture stability plan 2026-06-11): single source of truth for
foreman label strings + their classification. Replaces the
hand-maintained ``_BLOCKING_LABELS`` tuple in ``reconciler/rules.py`` and
the scattered string literals across role + reconciler modules.

The original Decision 1 motivation: foreman#194 was a class-attribute
``Labels`` resurrection that orphaned (see foreman#279). Decision 1
chose the StrEnum pattern over class attributes, mirroring the
foreman#258 ``Outcome`` enum design. Each member carries its own
:class:`LabelClass` classification; a contributor who adds a new member
as a bare string (``NEW = "new"`` instead of
``NEW = ("new", LabelClass.<bucket>)``) trips a ``TypeError`` at module
load — Python's enum machinery raises before any test gets a chance to
run.

Adding a new label:

1. Add one line to :class:`Label`:
   ``NEW = ("foreman:new", LabelClass.<bucket>)``.
2. Mirror the entry in :data:`init._FOREMAN_LABELS` with color +
   description (init owns CREATION metadata; this module owns the
   string + classification).
3. The relevant derived frozenset (:data:`QUEUE_LABELS`,
   :data:`IN_FLIGHT_LABELS`, :data:`BLOCKING_LABELS`,
   :data:`COUNTER_LABELS`, :data:`TERMINAL_LABELS`) updates
   automatically. No separate constant to keep in sync.
"""

from __future__ import annotations

from enum import Enum, StrEnum


class LabelClass(Enum):
    """The five classification buckets a :class:`Label` can belong to.

    QUEUE: ticket is queued for role X to act on. ``next_action`` reads
    these to dispatch the next role.

    IN_FLIGHT: a role is currently running. Reconciler observes to
    enforce one-role-at-a-time per ticket.

    BLOCKING: pauses the loop. Includes manual holds (``foreman:hold``),
    surfacings to human (``foreman:needs-help``), and fix-needed states
    (``foreman:spec-fix``, ``foreman:impl-fix``). Was the
    ``_BLOCKING_LABELS`` tuple in ``reconciler/rules.py`` pre-D1.

    COUNTER: attempt-N markers. Stats / retry-limit code uses these to
    count cycles. Membership check replaces regex-matching against
    label names in code that doesn't need to extract the number itself.

    TERMINAL: ``foreman:done`` clears the ticket cleanly;
    ``foreman:failed`` clears + marks abandoned after exhausting
    retries.
    """

    QUEUE = "queue"
    IN_FLIGHT = "in_flight"
    BLOCKING = "blocking"
    COUNTER = "counter"
    TERMINAL = "terminal"


class Label(StrEnum):
    """Every ``foreman:*`` label string written to a GitHub issue or PR.

    ``StrEnum`` so members compare equal to their string values —
    PyGithub's ``add_to_labels`` and ``remove_from_labels`` receive the
    value verbatim, and f-string interpolation produces the bare
    string. Use ``.value`` explicitly when interpolating into SQL DDL
    (where the result is parsed by SQLite without going through the
    binder) so the format cannot accidentally produce ``"Label.PLAN"``
    instead of ``"foreman:plan"``.

    Each member's right-hand side is a tuple of
    ``(value: str, classification: LabelClass)``. The custom
    :meth:`__new__` requires the classification — omitting it raises
    a ``TypeError`` at class-creation time, before module load
    completes.
    """

    classification: LabelClass

    def __new__(cls, value: str, classification: LabelClass) -> Label:
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.classification = classification
        return obj

    # QUEUE — ticket queued for role X
    PLAN = ("foreman:plan", LabelClass.QUEUE)
    PLAN_APPROVED = ("foreman:plan-approved", LabelClass.QUEUE)
    IMPL_REVIEW = ("foreman:impl-review", LabelClass.QUEUE)
    IMPL_APPROVED = ("foreman:impl-approved", LabelClass.QUEUE)

    # IN_FLIGHT — role currently running on this ticket
    PLANNING = ("foreman:planning", LabelClass.IN_FLIGHT)
    MERGING_PLAN = ("foreman:merging-plan", LabelClass.IN_FLIGHT)
    MERGING_IMPL = ("foreman:merging-impl", LabelClass.IN_FLIGHT)

    # BLOCKING — pauses the loop
    HOLD = ("foreman:hold", LabelClass.BLOCKING)
    NEEDS_HELP = ("foreman:needs-help", LabelClass.BLOCKING)
    SPEC_FIX = ("foreman:spec-fix", LabelClass.BLOCKING)
    IMPL_FIX = ("foreman:impl-fix", LabelClass.BLOCKING)

    # COUNTER — attempt markers
    IMPL_ATTEMPT_1 = ("foreman:impl-attempt-1", LabelClass.COUNTER)
    IMPL_ATTEMPT_2 = ("foreman:impl-attempt-2", LabelClass.COUNTER)
    IMPL_ATTEMPT_3 = ("foreman:impl-attempt-3", LabelClass.COUNTER)
    FIX_ATTEMPT_1 = ("foreman:fix-attempt-1", LabelClass.COUNTER)
    FIX_ATTEMPT_2 = ("foreman:fix-attempt-2", LabelClass.COUNTER)
    FIX_ATTEMPT_3 = ("foreman:fix-attempt-3", LabelClass.COUNTER)

    # TERMINAL — final state
    DONE = ("foreman:done", LabelClass.TERMINAL)
    FAILED = ("foreman:failed", LabelClass.TERMINAL)


QUEUE_LABELS: frozenset[str] = frozenset(
    m.value for m in Label if m.classification is LabelClass.QUEUE
)
"""String values whose classification is :attr:`LabelClass.QUEUE`.

The labels ``next_action`` consults to decide which role to dispatch.
"""

IN_FLIGHT_LABELS: frozenset[str] = frozenset(
    m.value for m in Label if m.classification is LabelClass.IN_FLIGHT
)
"""String values whose classification is :attr:`LabelClass.IN_FLIGHT`.

The labels the reconciler observes to enforce one-role-at-a-time.
"""

BLOCKING_LABELS: frozenset[str] = frozenset(
    m.value for m in Label if m.classification is LabelClass.BLOCKING
)
"""String values whose classification is :attr:`LabelClass.BLOCKING`.

Replaces the hand-maintained ``_BLOCKING_LABELS`` tuple in
``reconciler/rules.py``. Any rate-limit / blocked-state check reads
this directly.
"""

COUNTER_LABELS: frozenset[str] = frozenset(
    m.value for m in Label if m.classification is LabelClass.COUNTER
)
"""String values whose classification is :attr:`LabelClass.COUNTER`.

Attempt markers stamped per cycle. Code that constructs label names
via f-strings (``f"foreman:impl-attempt-{n}"``) or extracts numbers via
regex still does so; this set is for membership checks and tests that
need to enumerate all attempt labels.
"""

TERMINAL_LABELS: frozenset[str] = frozenset(
    m.value for m in Label if m.classification is LabelClass.TERMINAL
)
"""String values whose classification is :attr:`LabelClass.TERMINAL`."""


__all__ = [
    "BLOCKING_LABELS",
    "COUNTER_LABELS",
    "IN_FLIGHT_LABELS",
    "Label",
    "LabelClass",
    "QUEUE_LABELS",
    "TERMINAL_LABELS",
]
