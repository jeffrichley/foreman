"""Keystone meta-tests for ``foreman.labels`` — issue #194.

Foreman's ``foreman:*`` GitHub labels were previously referenced as raw
string literals scattered across 12+ production files, with three role
modules each maintaining their own private ``_LABEL_*`` constants.
Renaming a label required editing every file in lockstep; a typo in a
rule predicate was undetectable until runtime (the predicate would just
silently never match). Issue #194 centralized the names into a single
:class:`foreman.labels.Labels` catalog. This module is the canary that
catches drift between the catalog and the v3 consumers.

Two failure modes the tests catch:

1. **Typo-in-rule drift**: a rule predicate accidentally writes
   ``"foreman:impl-reveiw"``. The literal would never match a real
   label, so the rule stops firing. Test 1 walks every v3 catalog
   ``.py`` file with ``ast.walk``, collects every ``ast.Constant`` str
   whose value starts with ``"foreman:"``, and asserts that union is a
   subset of ``Labels.all() | {prefixes}``. A typo fails the test with
   the offending ``file:line`` in the message.

2. **Dead-label drift**: ``Labels.SPEC_FIX`` exists in the catalog but
   no live v3 file references it. Test 2 walks the same catalog files
   looking for each ``Labels.all()`` value (as either a ``str``
   constant OR a ``Labels.<UPPER>`` attribute access) and fails if any
   has zero references. A label that's declared but consumed by
   nothing means a rule the catalog assumes is alive isn't actually
   live; deleting the label here or wiring up the consumer is the fix.

**Scope exclusion (v2-deprecated modules)**: The test deliberately skips
``packages/foreman/src/foreman/daemon.py`` (deprecated v2 per its own
module docstring at lines 1-12), ``dispatcher.py`` (v2 next-action
state machine), ``daemon_runners.py`` (v2 wrapper), the package-root
``worker.py`` (v2 worker; NOT ``roles/worker.py``), and the v2-only
plumbing modules (``poller.py``, ``queue.py``, ``role_dispatch.py``,
``locks.py``, ``storage.py``). These modules reference labels that
don't exist in the v3 catalog (``foreman:implementing``,
``foreman:spec-ready``, etc.) and are flagged for removal in a separate
follow-up PR. Refactoring them now would pollute :class:`Labels` with
dead state; the exclusion is by module category, not by current literal
count. When the v2 removal PR lands, the names come out of
``_V2_DEPRECATED_FILES``.

**Scope exclusion (tests)**: The test deliberately skips
``packages/foreman/tests/`` because tests intentionally pin the literal
string contract for regression guard; refactoring them to use
``Labels`` is explicitly out of scope per the issue body.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from foreman import init
from foreman.labels import Labels

# Repo-relative roots so the test is portable across worktree paths.
_FOREMAN_SRC = Path(__file__).resolve().parents[1] / "src" / "foreman"

# The seven production files that DO drive the v3 reconciler / role / init
# pipeline. The keystone test scans these — and ONLY these. Adding a new v3
# consumer means adding the path here so the literal-scan + dead-label-scan
# stay comprehensive.
_V3_CATALOG_FILES: tuple[Path, ...] = (
    _FOREMAN_SRC / "init.py",
    _FOREMAN_SRC / "reconciler" / "rules.py",
    _FOREMAN_SRC / "reconciler" / "actions.py",
    _FOREMAN_SRC / "reconciler" / "observer.py",
    _FOREMAN_SRC / "reconciler" / "daemon.py",
    _FOREMAN_SRC / "roles" / "worker.py",
    _FOREMAN_SRC / "roles" / "reviewer.py",
    _FOREMAN_SRC / "roles" / "fixer.py",
)

# v2-deprecated modules listed for documentation; the catalog scan only
# walks ``_V3_CATALOG_FILES``, so these never get touched. The set is a
# ``frozenset`` so a future cleanup PR that deletes the v2 modules
# removes names from this constant rather than scattering grep / path-
# filter logic across the test body.
_V2_DEPRECATED_FILES: frozenset[str] = frozenset(
    {
        "daemon.py",
        "daemon_host.py",  # v2 host adapter consumed only by v2 cli paths
        "dispatcher.py",
        "daemon_runners.py",
        "worker.py",  # package-root v2 worker, NOT roles/worker.py
        "poller.py",
        "queue.py",
        "role_dispatch.py",
        "locks.py",
        "storage.py",
    }
)

# Allowed-set for Test 1: every catalog label PLUS the two attempt
# prefixes (which appear as string fragments inside :class:`Labels` and
# inside ``.startswith(...)`` callers). The prefixes are NOT real
# labels (``Labels.all()`` does not include them), so we hoist them
# into the allow-set explicitly to avoid false positives.
_ALLOWED_LABEL_LITERALS: frozenset[str] = frozenset(
    set(Labels.all()) | {Labels.IMPL_ATTEMPT_PREFIX, Labels.FIX_ATTEMPT_PREFIX}
)


def _collect_foreman_str_literals(path: Path) -> list[tuple[int, str]]:
    """Walk ``path``'s AST and return ``(lineno, value)`` for every
    ``str`` constant whose value starts with ``"foreman:"``.

    Used by Test 1 to find rule-predicate typos. Returns ``(lineno,
    value)`` so the failure message can point the operator at the
    exact source line.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("foreman:"):
                hits.append((node.lineno, node.value))
    return hits


def _file_references_label(path: Path, label_name: str) -> bool:
    """Return True if ``path``'s AST contains either:

    - an ``ast.Constant`` whose ``value`` equals ``label_name`` (covers
      the during-refactor state where init still has literals in the
      ``_LABEL_METADATA`` keys via ``Labels.X``-evaluated form on the
      module's bytecode — see Test 3 for the structural check there);
    - an ``ast.Attribute`` whose attribute chain ends in the matching
      ``Labels.<UPPER>`` name (the post-refactor steady state where
      every reference is ``Labels.X``).

    Either shape proves the label is consumed by this file. Used by
    Test 2 to detect dead labels.
    """
    # Build the set of ``Labels.<UPPER>`` attribute names that would
    # produce ``label_name`` at runtime. We resolve them dynamically so a
    # rename inside :class:`Labels` automatically updates the matcher.
    matching_attrs = {
        attr
        for attr in dir(Labels)
        if not attr.startswith("_")
        and isinstance(getattr(Labels, attr, None), str)
        and getattr(Labels, attr) == label_name
    }
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == label_name:
            return True
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "Labels"
            and node.attr in matching_attrs
        ):
            return True
    return False


def test_every_foreman_label_literal_in_v3_catalog_matches_labels_class() -> None:
    """Drift guard #1: every ``"foreman:..."`` string literal in v3
    production code must match a value in :class:`Labels` (or one of
    the two attempt-counter prefixes).

    Catches typos like ``"foreman:impl-reveiw"`` that would silently
    stop a rule predicate from firing. Failure message names the
    offending ``file:line`` and the unmatched literal.
    """
    offenders: list[str] = []
    for path in _V3_CATALOG_FILES:
        for lineno, value in _collect_foreman_str_literals(path):
            if value not in _ALLOWED_LABEL_LITERALS:
                offenders.append(f"{path.name}:{lineno} {value!r}")
    assert offenders == [], (
        "found 'foreman:...' string literals in v3 production code that do not "
        "match any value in Labels.all() or the attempt-counter prefixes:\n  "
        + "\n  ".join(offenders)
        + "\nThese are likely rule-predicate typos. If a new label was "
        "intentionally added, register it in foreman.labels.Labels first."
    )


def test_every_label_in_catalog_is_referenced_by_a_live_consumer() -> None:
    """Drift guard #2: every value in :meth:`Labels.all` must be
    referenced by at least one v3 catalog file (literal OR
    ``Labels.<UPPER>`` attribute).

    Catches dead labels: a name declared in :class:`Labels` but with
    no live consumer means a future rule the catalog assumes is alive
    isn't actually wired. Deleting the dead label OR wiring up the
    consumer fixes the test.

    The six ``IMPL_ATTEMPT_*`` / ``FIX_ATTEMPT_*`` constants are
    EXEMPTED from this check: they're never referenced as
    ``Labels.IMPL_ATTEMPT_1`` in production code, because the
    parameterized callers use ``Labels.impl_attempt(attempt)`` instead
    and the ``.startswith()`` callers use ``Labels.IMPL_ATTEMPT_PREFIX``
    / ``Labels.FIX_ATTEMPT_PREFIX``. The two prefix constants ARE
    referenced in the live ``.startswith()`` call sites, so they're
    naturally covered.
    """
    exempted = {
        Labels.IMPL_ATTEMPT_1,
        Labels.IMPL_ATTEMPT_2,
        Labels.IMPL_ATTEMPT_3,
        Labels.FIX_ATTEMPT_1,
        Labels.FIX_ATTEMPT_2,
        Labels.FIX_ATTEMPT_3,
    }
    dead: list[str] = []
    for label_name in Labels.all():
        if label_name in exempted:
            continue
        referenced = any(
            _file_references_label(path, label_name) for path in _V3_CATALOG_FILES
        )
        if not referenced:
            dead.append(label_name)
    assert dead == [], (
        "found labels in Labels.all() with no live consumer in any v3 "
        "catalog file:\n  "
        + "\n  ".join(dead)
        + "\nEither delete the dead label from foreman.labels.Labels OR "
        "wire up a consumer in init.py / reconciler / a role module."
    )


def test_init_label_catalog_covers_labels_all() -> None:
    """Drift guard #3: ``init.py``'s ``_FOREMAN_LABELS`` + ``_LABEL_METADATA``
    must stay aligned with :meth:`Labels.all`.

    Two assertions:

    1. ``[name for name, _, _ in init._FOREMAN_LABELS] == Labels.all()``
       — order matters; the operator-facing init summary lists labels
       in this order and the ``_ensure_labels`` loop walks them in this
       order.
    2. ``set(init._LABEL_METADATA.keys()) == set(Labels.all())`` — every
       label needs a color + description registered; adding a label to
       :class:`Labels` without metadata is a test failure (this is the
       "rule could check a label init.py doesn't create" invariant
       called out in the issue body).
    """
    names_from_foreman_labels = [name for name, _, _ in init._FOREMAN_LABELS]
    assert names_from_foreman_labels == Labels.all(), (
        "init._FOREMAN_LABELS no longer aligns with Labels.all(); either "
        "the order drifted or one side gained / lost a label. "
        f"_FOREMAN_LABELS names: {names_from_foreman_labels}\n"
        f"Labels.all():          {Labels.all()}"
    )
    metadata_keys = set(init._LABEL_METADATA.keys())
    expected = set(Labels.all())
    missing = expected - metadata_keys
    extra = metadata_keys - expected
    assert metadata_keys == expected, (
        "init._LABEL_METADATA keys drifted from Labels.all(); "
        f"missing color/description for: {sorted(missing)}, "
        f"extra (unknown to Labels): {sorted(extra)}"
    )


@pytest.mark.parametrize(
    "method,prefix",
    [
        (Labels.impl_attempt, Labels.IMPL_ATTEMPT_PREFIX),
        (Labels.fix_attempt, Labels.FIX_ATTEMPT_PREFIX),
    ],
)
def test_attempt_helpers_build_prefix_compatible_names(method, prefix: str) -> None:
    """The parameterized ``Labels.impl_attempt(n)`` / ``Labels.fix_attempt(n)``
    helpers must produce names that ``startswith(prefix)`` would match.

    This guards against an unintended divergence between the
    constructed name and the prefix the .startswith() callers filter
    against; if the two drifted, rule predicates would silently miss
    in-flight attempt counters.
    """
    for n in (1, 2, 3, 7):
        assert method(n).startswith(prefix)
        assert method(n) == f"{prefix}{n}"
