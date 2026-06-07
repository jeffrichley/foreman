"""Single source of truth for Foreman's ``foreman:*`` GitHub label names.

Issue #194: prior to this module, the v3 reconciler + role + init code paths
referenced label names as raw string literals scattered across 12+ files,
with three role modules each maintaining their own private ``_LABEL_*``
constants. Renaming a label required editing every file in lockstep; a
typo in a rule predicate was undetectable until runtime (the predicate
just silently never matched).

This module exports a single :class:`Labels` catalog. Every consumer
imports from here:

    from foreman.labels import Labels

    if Labels.HOLD in issue.labels:
        ...

    repo.create_label(name=Labels.NEEDS_HELP, ...)

    for label in attempt_labels:
        if label.startswith(Labels.IMPL_ATTEMPT_PREFIX):
            ...

The keystone test in ``packages/foreman/tests/test_labels_keystone.py``
walks every v3 consumer file and asserts:

    1. Every ``"foreman:..."`` literal in production code matches a
       value in :meth:`Labels.all` (catches typos in rule predicates).
    2. Every value in :meth:`Labels.all` is referenced by at least one
       live consumer (catches dead labels that no rule fires on).

This module is a LEAF: it imports only from ``__future__`` and the
standard library. Importing it must not pull in any other ``foreman.*``
module — that's what makes it safe to import from anywhere in the
package (including ``init.py``, the reconciler core, and the role
modules) without circular-import risk.

:class:`Labels` is intentionally NOT re-exported from
``foreman.__init__``. The full import path (``from foreman.labels
import Labels``) is the operator-facing contract; mirrors how the rest
of the package avoids name-leak in the top-level namespace.
"""

from __future__ import annotations


class Labels:
    """Catalog of every ``foreman:*`` GitHub label v3 references.

    Attribute access (``Labels.SPEC_FIX``) returns the canonical string
    (``"foreman:spec-fix"``). :meth:`all` returns the full operator-
    meaningful ordering (state labels in v3 pipeline order, then
    modifier labels, then the six attempt counters); used by
    ``init.py`` to drive label registration and by the keystone test
    to enforce the catalog contract.

    Attempt counters (``IMPL_ATTEMPT_N`` / ``FIX_ATTEMPT_N``) are
    enumerated for parity with :data:`init._FOREMAN_LABELS`. Callers
    that build the label at runtime from an integer use the
    parameterized helpers :meth:`impl_attempt` and :meth:`fix_attempt`
    instead; callers that filter against the prefix use
    :attr:`IMPL_ATTEMPT_PREFIX` / :attr:`FIX_ATTEMPT_PREFIX` with
    ``str.startswith``.

    The class is not instantiated; treat it as a namespaced constant
    catalog. All attributes are class-level.
    """

    # ------------------------------------------------------------------
    # State labels (v3 pipeline order)
    # ------------------------------------------------------------------

    PLAN: str = "foreman:plan"
    PLANNING: str = "foreman:planning"
    PLAN_APPROVED: str = "foreman:plan-approved"
    MERGING_PLAN: str = "foreman:merging-plan"
    SPEC_FIX: str = "foreman:spec-fix"
    IMPL_REVIEW: str = "foreman:impl-review"
    IMPL_APPROVED: str = "foreman:impl-approved"
    MERGING_IMPL: str = "foreman:merging-impl"
    IMPL_FIX: str = "foreman:impl-fix"

    # ------------------------------------------------------------------
    # Modifier labels (operator + escalation)
    # ------------------------------------------------------------------

    NEEDS_HELP: str = "foreman:needs-help"
    HOLD: str = "foreman:hold"
    DONE: str = "foreman:done"
    FAILED: str = "foreman:failed"

    # ------------------------------------------------------------------
    # Attempt-counter prefixes — used by callers that filter against
    # ``str.startswith``. These are NOT labels in their own right;
    # they're string fragments. ``Labels.all()`` does not include them.
    # ------------------------------------------------------------------

    IMPL_ATTEMPT_PREFIX: str = "foreman:impl-attempt-"
    FIX_ATTEMPT_PREFIX: str = "foreman:fix-attempt-"

    # ------------------------------------------------------------------
    # Attempt-counter labels — enumerated for parity with
    # :data:`init._FOREMAN_LABELS`. Runtime-built variants come from
    # :meth:`impl_attempt` / :meth:`fix_attempt` instead.
    # ------------------------------------------------------------------

    IMPL_ATTEMPT_1: str = "foreman:impl-attempt-1"
    IMPL_ATTEMPT_2: str = "foreman:impl-attempt-2"
    IMPL_ATTEMPT_3: str = "foreman:impl-attempt-3"
    FIX_ATTEMPT_1: str = "foreman:fix-attempt-1"
    FIX_ATTEMPT_2: str = "foreman:fix-attempt-2"
    FIX_ATTEMPT_3: str = "foreman:fix-attempt-3"

    @classmethod
    def all(cls) -> list[str]:
        """Return every v3 label name in operator-meaningful order.

        Order mirrors :data:`init._FOREMAN_LABELS`: state labels in
        v3 pipeline order, then modifier labels, then the six attempt
        counters. The order is byte-identical to today's so the init
        summary lists labels in the same sequence and the keystone
        test against ``_FOREMAN_LABELS`` survives without resorting.

        Attempt-counter prefixes (:attr:`IMPL_ATTEMPT_PREFIX` /
        :attr:`FIX_ATTEMPT_PREFIX`) are intentionally NOT included —
        they're string fragments, not labels.
        """
        return [
            cls.PLAN,
            cls.PLANNING,
            cls.PLAN_APPROVED,
            cls.MERGING_PLAN,
            cls.SPEC_FIX,
            cls.IMPL_REVIEW,
            cls.IMPL_APPROVED,
            cls.MERGING_IMPL,
            cls.IMPL_FIX,
            cls.NEEDS_HELP,
            cls.HOLD,
            cls.DONE,
            cls.FAILED,
            cls.IMPL_ATTEMPT_1,
            cls.IMPL_ATTEMPT_2,
            cls.IMPL_ATTEMPT_3,
            cls.FIX_ATTEMPT_1,
            cls.FIX_ATTEMPT_2,
            cls.FIX_ATTEMPT_3,
        ]

    @classmethod
    def impl_attempt(cls, n: int) -> str:
        """Return the ``foreman:impl-attempt-<n>`` label for attempt ``n``.

        Used by ``roles.worker`` when stamping the per-cycle attempt
        marker. Equivalent to ``f"{cls.IMPL_ATTEMPT_PREFIX}{n}"``.
        """
        return f"{cls.IMPL_ATTEMPT_PREFIX}{n}"

    @classmethod
    def fix_attempt(cls, n: int) -> str:
        """Return the ``foreman:fix-attempt-<n>`` label for attempt ``n``.

        Used by ``roles.fixer`` when stamping the per-cycle attempt
        marker. Equivalent to ``f"{cls.FIX_ATTEMPT_PREFIX}{n}"``.
        """
        return f"{cls.FIX_ATTEMPT_PREFIX}{n}"


__all__ = ["Labels"]
