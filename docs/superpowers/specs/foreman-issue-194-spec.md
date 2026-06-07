# Spec: centralize `foreman:*` label constants into one source of truth (issue #194)

## Goal

Eliminate the drift risk where Foreman's `foreman:*` GitHub labels are
referenced as raw string literals (127 occurrences across 12 production
files) and DUPLICATED as private `_LABEL_*` constants inside three role
modules. Introduce a new leaf module `packages/foreman/src/foreman/labels.py`
that exports a single `Labels` catalog, refactor the v3 reconciler + role
+ init code paths to import from it, and add a keystone test that fails on
both new drift (a label literal not in the catalog) AND dead labels (a
catalog entry referenced by no live v3 consumer). Pure refactor — no label
strings change, no behavior changes. Addresses issue
[#194](https://github.com/jeffrichley/foreman/issues/194).

## Acceptance criteria

- New leaf module `packages/foreman/src/foreman/labels.py` exporting a
  frozen `Labels` class with UPPER_CASE attribute constants whose values
  are the canonical `foreman:*` strings currently registered in
  `init.py:_FOREMAN_LABELS` (lines 78-123). At minimum the class exposes:
  `PLAN`, `PLANNING`, `PLAN_APPROVED`, `MERGING_PLAN`, `SPEC_FIX`,
  `IMPL_REVIEW`, `IMPL_APPROVED`, `MERGING_IMPL`, `IMPL_FIX`, `NEEDS_HELP`,
  `HOLD`, `DONE`, `FAILED`. Plus the six attempt-counter constants
  `IMPL_ATTEMPT_1`, `IMPL_ATTEMPT_2`, `IMPL_ATTEMPT_3`, `FIX_ATTEMPT_1`,
  `FIX_ATTEMPT_2`, `FIX_ATTEMPT_3`.
- `Labels` also exposes the two attempt-counter prefixes
  `IMPL_ATTEMPT_PREFIX = "foreman:impl-attempt-"` and
  `FIX_ATTEMPT_PREFIX = "foreman:fix-attempt-"` for callers that use
  `.startswith(...)` on issue-label names (today: `rules.py:500-501`,
  `roles/worker.py:918,999`, `roles/fixer.py:600,638`).
- `Labels` also exposes parameterized helpers
  `Labels.impl_attempt(n: int) -> str` and `Labels.fix_attempt(n: int) -> str`
  that return `f"{IMPL_ATTEMPT_PREFIX}{n}"` / `f"{FIX_ATTEMPT_PREFIX}{n}"`
  respectively, for callers that build the label at runtime from an
  `attempt` int (today: `roles/worker.py:702`, `roles/fixer.py:483`).
- `Labels.all()` classmethod returns a `list[str]` of every v3 label name
  in the same operator-meaningful order as today's `_FOREMAN_LABELS`
  (state labels first in v3 pipeline order, then modifier labels, then
  the six attempt counters). The attempt-counter prefixes are NOT
  included in `Labels.all()` (they're not labels, they're string fragments).
- `packages/foreman/src/foreman/labels.py` has ZERO imports from other
  `foreman.*` modules. It's a leaf module so it can be imported by any
  consumer without circular-import risk. It may import from `dataclasses`,
  `typing`, `__future__`, and stdlib only.
- `Labels` is NOT exported from `foreman/__init__.py`. Consumers import
  it explicitly: `from foreman.labels import Labels`. (Mirrors how the
  rest of the package avoids name-leak in the top-level namespace; see
  the empty-ish `packages/foreman/src/foreman/__init__.py`.)
- `init.py:_FOREMAN_LABELS` (currently `list[tuple[str, str, str]]` at
  lines 78-123) is rebuilt to derive the label name column from
  `Labels.all()`. The colors and descriptions stay where they are
  today — extracted into a new module-private
  `_LABEL_METADATA: dict[str, tuple[str, str]]` map keyed by label name
  with value `(color, description)`. `_FOREMAN_LABELS` becomes a derived
  property/list comprehension: `[(name, *_LABEL_METADATA[name]) for name
  in Labels.all()]`. The ORDER of `_FOREMAN_LABELS` must be byte-identical
  to today's (state → modifier → attempt counters) — the operator-facing
  summary in `_format_summary` lists them in this order, and the
  `_ensure_labels` loop walks them in this order.
- A new module-level assertion (or short test) confirms
  `_LABEL_METADATA` covers exactly `set(Labels.all())` — no extra keys,
  no missing keys. Choice between a `__init__`-time `assert` and a
  dedicated unit test is up to the Worker; the keystone test catches
  the regression at test-time either way.
- Every production-code reference to a `"foreman:..."` string literal in
  the v3 catalog of files below is replaced with an import from
  `foreman.labels.Labels`. **Scope (v3 catalog files)**:
  - `packages/foreman/src/foreman/init.py`
  - `packages/foreman/src/foreman/reconciler/rules.py`
  - `packages/foreman/src/foreman/reconciler/actions.py`
  - `packages/foreman/src/foreman/reconciler/observer.py`
  - `packages/foreman/src/foreman/roles/worker.py`
  - `packages/foreman/src/foreman/roles/reviewer.py`
  - `packages/foreman/src/foreman/roles/fixer.py`
- The per-role private `_LABEL_*` constants in `roles/worker.py`
  (lines 111-116), `roles/reviewer.py` (lines 65-70), and
  `roles/fixer.py` (lines 97-110) are DELETED. References that used
  them are switched to `Labels.SPEC_FIX` etc. Don't leave parallel
  sources of truth.
- `roles/reviewer.py:_REVIEWER_ENTRY_LABEL_BY_TARGET` (line 80) and
  `roles/fixer.py:_FIXER_ENTRY_LABEL_BY_TARGET` (line 112) and
  `roles/worker.py:_WORKER_ENTRY_LABELS` (line 112) keep their existing
  shape but their values switch to `Labels.*` constants.
- `reconciler/actions.py:_MERGING_LABEL_FOR_TARGET` (lines 119-122) and
  the implicit attempt-merge needs-help body string (lines 256-278)
  switch to `Labels.MERGING_PLAN` / `Labels.MERGING_IMPL` and
  `Labels.NEEDS_HELP`.
- `reconciler/observer.py:_QUERY` (lines 49-93) currently embeds the
  filter-label list as raw strings inside a triple-quoted GraphQL
  template. The Worker switches this to a Python f-string (or a
  `_build_query()` helper) that interpolates the label names from
  `Labels` — the actual filter list must remain byte-identical (same
  labels in the same order) to preserve the observer's existing GraphQL
  contract. Acceptable shapes: (a) f-string at module top with explicit
  `{Labels.PLAN!r}, {Labels.PLANNING!r}, ...`; or (b) a small builder
  function called once at import time; (c) a list-join of
  `[Labels.PLAN, Labels.PLANNING, ...]` interpolated into the template.
  Worker's choice — whichever reads cleanest.
- New keystone test `packages/foreman/tests/test_labels_keystone.py`
  with the docstring referencing this spec + issue #194:
  - Test 1 — `test_every_foreman_label_literal_in_v3_catalog_matches_labels_class`:
    walks every `.py` file under `packages/foreman/src/foreman/` that is
    in the v3 catalog file list above (state the list explicitly as a
    module-private tuple in the test), uses `ast.walk` to collect every
    `ast.Constant` of type `str` whose value starts with `"foreman:"`,
    and asserts the union of those strings is a subset of
    `set(Labels.all()) | {Labels.IMPL_ATTEMPT_PREFIX, Labels.FIX_ATTEMPT_PREFIX}`.
    The prefixes are included in the allowed-set so an AST node like
    `"foreman:impl-attempt-"` (the prefix used inside Labels itself or
    inside a `.startswith()` arg) doesn't trigger a false positive.
    Failure message names the offending file:line and the unmatched
    literal so the operator can fix it inline.
  - Test 2 — `test_every_label_in_catalog_is_referenced_by_a_live_consumer`:
    for each name in `Labels.all()`, walks the same v3 catalog file
    list AND asserts at least one file contains an `ast.Constant`
    whose value equals that name OR an `ast.Attribute` whose attribute
    chain ends in the matching `Labels.<UPPER>` (covers both the
    refactor's during-transition state and the post-refactor state
    where every reference is `Labels.X`). Catches dead labels: a label
    declared in `Labels.all()` but referenced by no live module fails
    the test. The six attempt-counter constants are exempted from this
    check because they're referenced indirectly through
    `IMPL_ATTEMPT_PREFIX` + `.startswith()` patterns; the test's
    docstring documents this exemption explicitly.
  - Test 3 — `test_init_label_catalog_covers_labels_all`: asserts
    `[name for name, _, _ in init._FOREMAN_LABELS] == Labels.all()`
    AND `set(init._LABEL_METADATA.keys()) == set(Labels.all())`. This
    is the invariant the issue requested ("rule could check a label
    `init.py` doesn't create").
- The keystone test file imports `Labels` via the public import path
  (`from foreman.labels import Labels`) and reaches the
  `_LABEL_METADATA` / `_FOREMAN_LABELS` internals via direct module
  access (`from foreman import init`). The test relies on
  `pkgutil.iter_modules` only if absolutely needed; otherwise the file
  list is hardcoded for stability (mirrors how `test_env_scrub_keystone`
  hardcodes its scrub list).
- The keystone test does NOT scan test files (`packages/foreman/tests/`).
  Tests intentionally pin the literal string contract for regression
  guard; refactoring them is explicitly out of scope per the issue body.
- The keystone test does NOT scan v2-deprecated modules. Specifically
  excluded: `packages/foreman/src/foreman/daemon.py` (deprecated v2 per
  its own module docstring), `dispatcher.py` (v2 next-action state
  machine), `daemon_runners.py` (v2 wrapper), `worker.py` at the
  package root (v2 worker; NOT `roles/worker.py`), plus the v2-only
  `poller.py`, `queue.py`, `role_dispatch.py`, `locks.py`, and
  `storage.py` (they may or may not contain literals; the exclusion
  is by module category, not by current literal count). These modules
  retain raw string literals; v2 removal is a separate follow-up
  ticket (see the deprecation note at `daemon.py:1-12`). The test
  module docstring explicitly names this exclusion + its rationale.
- The test file's exclusion list is implemented as a module-private
  constant `_V2_DEPRECATED_FILES: frozenset[str]` so a future cleanup
  PR that deletes the v2 modules removes the names from this set
  rather than scattering grep/path-filter logic.
- `just check` passes: ruff + mypy + the full pytest suite (~924 tests).
  Verify before claiming done.

## Approach

This is a pure mechanical refactor. The interesting design choice is the
shape of `Labels`; everything else is "find and replace, then re-run the
suite."

### Why a frozen class with attributes (vs. enum, vs. module-level constants)

The issue body recommends "a frozen class with UPPER_CASE attributes" and
that recommendation is right for this codebase:

- A `StrEnum` would force every consumer to write
  `Labels.SPEC_FIX.value` to get the string, which is noisy and
  introduces a `.value` footgun if a caller forgets it (the equality
  check `label == Labels.SPEC_FIX` works against a `StrEnum`, but the
  `set` membership check `Labels.SPEC_FIX in some_str_set` does NOT —
  this codebase has both patterns).
- Module-level constants (e.g., `LABEL_SPEC_FIX = "foreman:spec-fix"`)
  scatter the catalog across the module's top-level namespace and make
  `Labels.all()` awkward (would need to introspect the module).
- A frozen class gives namespaced access (`Labels.SPEC_FIX`), a clean
  classmethod surface (`Labels.all()`), and supports both static
  references (`Labels.SPEC_FIX`) and dynamic-by-name lookup
  (`getattr(Labels, "SPEC_FIX")`) without needing `.value`.

Implementation: use `@dataclass(frozen=True)` with class-level attribute
assignments, OR a plain class with `__init__ = None` and class-level
constants. Both are idiomatic; the Worker's choice. The minimum viable
shape:

```python
class Labels:
    PLAN: str = "foreman:plan"
    PLANNING: str = "foreman:planning"
    # ... etc
    IMPL_ATTEMPT_PREFIX: str = "foreman:impl-attempt-"
    FIX_ATTEMPT_PREFIX: str = "foreman:fix-attempt-"
    IMPL_ATTEMPT_1: str = f"{IMPL_ATTEMPT_PREFIX}1"  # or just literal
    # ...

    @classmethod
    def all(cls) -> list[str]:
        return [cls.PLAN, cls.PLANNING, ...]

    @classmethod
    def impl_attempt(cls, n: int) -> str:
        return f"{cls.IMPL_ATTEMPT_PREFIX}{n}"

    @classmethod
    def fix_attempt(cls, n: int) -> str:
        return f"{cls.FIX_ATTEMPT_PREFIX}{n}"
```

### Why a separate `_LABEL_METADATA` map in `init.py`

The colors and descriptions are operator-facing init concerns that don't
belong in the leaf `labels.py` module:

- `labels.py` stays minimal so any module can import it without dragging
  along init's render-summary copy text.
- Colors and descriptions can change without touching `labels.py` (e.g.,
  if we ever recolor `foreman:hold` for accessibility, that lives in
  `init.py` where the GitHub-create call is).
- A separate map also lets the keystone test assert
  `set(_LABEL_METADATA) == set(Labels.all())` — making "I added a label
  to `Labels` but forgot to register it with a color/description"
  detectable at test time.

### Why scoping to v3 catalog files only

The issue lists exactly 7 consumer files for the dead-label check
(`init.py`, `reconciler/{rules,actions,observer}.py`,
`roles/{worker,reviewer,fixer}.py`). These are the live v3 reconciler
plus init. The v2 modules (`daemon.py`, `dispatcher.py`,
`daemon_runners.py`, the package-root `worker.py`, plus the v2 plumbing
modules) reference labels that don't exist in the v3 catalog
(`foreman:implementing`, `foreman:implementing-ready`,
`foreman:ready-for-merge`, `foreman:spec-ready`, `foreman:spec-review`)
AND are explicitly DEPRECATED per `daemon.py:1-12` ("stays in tree until
v3 is proven stable for ~2 weeks post-cutover; then this module +
foreman.storage will be removed in a follow-up PR"). Refactoring them
to use `Labels` would either (a) require adding v2-only labels to
`Labels` — polluting the v3 catalog with dead state — or (b) require
a parallel `Labels.Deprecated` namespace that gets `git rm`'d in the v2
removal PR anyway. Both add churn for negative long-term value. We skip
them with an explicit exclusion documented in the test.

### Why the keystone test has two complementary assertions

The issue describes two distinct drift failures:

1. **Typo-in-rule drift**: a rule predicate says `"foreman:impl-reveiw"`
   and silently never matches. The literal-scan assertion catches this:
   the typo isn't in `Labels.all()`, so the test fails with file:line.
2. **Dead-label drift**: `Labels.SPEC_FIX` exists in the catalog but no
   live v3 file references it. The catalog-coverage assertion catches
   this: the second test walks the consumer files looking for each
   `Labels.all()` value and fails if any has zero references.

Both assertions are pure-static analysis (ast.walk + path filter), so
the test is fast and deterministic. No network, no GitHub, no daemon.

### Why the test exempts attempt counters from the dead-label check

The six `IMPL_ATTEMPT_*` / `FIX_ATTEMPT_*` constants are never
referenced as `Labels.IMPL_ATTEMPT_1` in the production code — the
parameterized callers use `Labels.impl_attempt(attempt)` instead.
Without exemption, the second assertion would falsely flag all six as
dead. The test docstring documents the exemption (and the
`IMPL_ATTEMPT_PREFIX` / `FIX_ATTEMPT_PREFIX` constants ARE referenced
in the `.startswith()` callers, so the prefixes themselves are
naturally covered).

### Worker procedure (TDD-friendly order)

The Worker can land this in a single PR but should commit in a
deliberate order so any rollback unwinds cleanly:

1. **Create `labels.py`** with the full `Labels` class. No callers
   yet — the module is dead code until step 2. `just check` should
   still pass because the new module is import-only.
2. **Wire `init.py` through `Labels`**: replace the inline names in
   `_FOREMAN_LABELS` with a derived list built from `Labels.all()` +
   `_LABEL_METADATA`. Verify `_format_summary` still emits the same
   text (snapshot the operator-facing summary in a test if helpful).
3. **Add the keystone test** in failing form: write
   `test_labels_keystone.py` with the three subtests. Tests 1 + 3
   pass once `init.py` is wired. Test 2 will fail for every catalog
   label until step 4.
4. **Refactor reconciler/{rules,actions,observer}.py + roles/{worker,
   reviewer,fixer}.py** to import `Labels`. After each file is
   converted, run `pytest packages/foreman/tests/` to confirm no
   behavioral regression (label-shape tests pin the strings, so a
   typo during the rewrite shows up immediately).
5. **Delete the per-role `_LABEL_*` constants** in worker.py /
   reviewer.py / fixer.py. The keystone test confirms no stale
   parallel source of truth survived.
6. **Run `just check`** end-to-end. Resolve lint / mypy drift.
7. **Run the verification grep** from the issue body (adjusted to
   exclude v2 files):
   ```
   grep -r '"foreman:' packages/foreman/src/foreman/ \
     | grep -v -E '(labels\.py|daemon\.py|dispatcher\.py|daemon_runners\.py|^packages/foreman/src/foreman/worker\.py|poller\.py|queue\.py|role_dispatch\.py|locks\.py|storage\.py)' \
     | wc -l
   ```
   Should return 0 (modulo the prefix substrings inside `Labels`
   itself — `labels.py` is the source of truth and contains the
   literals).

## Sub-requests (topologically sorted)

1. Create `packages/foreman/src/foreman/labels.py` with the `Labels`
   class, the 13 v3 name constants, the 6 attempt-counter constants,
   the 2 prefix constants, the `all()` classmethod, and the
   `impl_attempt(n)` / `fix_attempt(n)` classmethods. ZERO imports from
   other `foreman.*` modules. Module docstring references issue #194 +
   the "one source of truth" contract.
2. Refactor `packages/foreman/src/foreman/init.py`:
   - Add `from foreman.labels import Labels` at the top.
   - Extract today's `_FOREMAN_LABELS` color + description columns
     into a new `_LABEL_METADATA: dict[str, tuple[str, str]]` map (keys
     match the existing `_FOREMAN_LABELS` order).
   - Rebuild `_FOREMAN_LABELS` as `[(name, *_LABEL_METADATA[name]) for
     name in Labels.all()]`. Verify the rebuilt list is byte-identical
     to today's at runtime (use a one-off `assert` during development,
     remove before commit).
3. Refactor `packages/foreman/src/foreman/reconciler/rules.py`:
   - `from foreman.labels import Labels` at the top.
   - Replace every `"foreman:..."` literal with `Labels.<UPPER>`. The
     `_PHASE_EXCLUDED` set at lines 483-496 becomes
     `_PHASE_EXCLUDED = {Labels.PLANNING, Labels.PLAN_APPROVED, ...}`.
   - Replace `.startswith("foreman:impl-attempt-")` / `.startswith(
     "foreman:fix-attempt-")` with
     `.startswith(Labels.IMPL_ATTEMPT_PREFIX)` /
     `.startswith(Labels.FIX_ATTEMPT_PREFIX)`.
4. Refactor `packages/foreman/src/foreman/reconciler/actions.py`:
   - Import `Labels`.
   - Replace literals in `_MERGING_LABEL_FOR_TARGET` (lines 119-122),
     `_surface_attempt_merge_needs_help` (lines 256-278), the
     `Action.SURFACE_HELP` branch (lines 328-344), the
     `ADVANCE_LABEL_TO_MERGING_*` / `ADVANCE_LABEL_TO_PLANNING` /
     `ADVANCE_LABEL_TO_PLAN_APPROVED` / `ADVANCE_LABEL_TO_DONE`
     branches (lines 376-437). The user-facing comment / log strings
     that EMBED a label name (e.g.,
     `"...label {merging_label}..."` at line 273) keep their f-string
     interpolation but read the value from `Labels.*`.
5. Refactor `packages/foreman/src/foreman/reconciler/observer.py`:
   - Import `Labels`.
   - Replace the inline label list in `_QUERY` (lines 56-67) with an
     f-string interpolation OR a builder function that joins
     `[Labels.PLAN, Labels.PLANNING, ...]` into the GraphQL filter
     array. The resulting query string MUST be byte-identical to
     today's (verify with a snapshot test if not already pinned).
6. Refactor `packages/foreman/src/foreman/roles/worker.py`:
   - Import `Labels`.
   - Replace the per-module `_LABEL_PLAN_APPROVED`, `_LABEL_IMPL_REVIEW`,
     `_LABEL_SPEC_FIX`, `_LABEL_NEEDS_HELP`, `_LABEL_FAILED` constants
     (lines 111-116) with direct `Labels.*` references at the call
     sites. Update `_WORKER_ENTRY_LABELS = frozenset({Labels.PLAN_APPROVED})`.
   - Replace the f-string `f"foreman:impl-attempt-{attempt}"` at line
     702 with `Labels.impl_attempt(attempt)`.
   - Replace the `.startswith("foreman:impl-attempt-")` predicates at
     lines 918 + 999 with `.startswith(Labels.IMPL_ATTEMPT_PREFIX)`.
   - DELETE the now-unused private `_LABEL_*` constant declarations.
7. Refactor `packages/foreman/src/foreman/roles/reviewer.py`:
   - Import `Labels`.
   - Replace per-module `_LABEL_SPEC_REVIEW`, `_LABEL_SPEC_READY`,
     `_LABEL_SPEC_FIX`, `_LABEL_IMPL_REVIEW`, `_LABEL_READY_FOR_MERGE`,
     `_LABEL_IMPL_FIX` (lines 65-70) at every reference. Note:
     `_LABEL_SPEC_REVIEW = "foreman:planning"` and
     `_LABEL_READY_FOR_MERGE = "foreman:impl-approved"` — preserve the
     mapping (so `Labels.PLANNING` and `Labels.IMPL_APPROVED` substitute
     in respectively).
   - Update `_REVIEWER_ENTRY_LABEL_BY_TARGET` (line 80) values to use
     `Labels.*`.
   - DELETE the now-unused private `_LABEL_*` constants.
8. Refactor `packages/foreman/src/foreman/roles/fixer.py`:
   - Import `Labels`.
   - Replace per-module `_LABEL_SPEC_FIX`, `_LABEL_PLANNING`,
     `_LABEL_NEEDS_HELP`, `_LABEL_FAILED`, `_LABEL_IMPL_FIX`,
     `_LABEL_IMPL_REVIEW` (lines 97-110) at every reference.
   - Update `_FIXER_ENTRY_LABEL_BY_TARGET` (line 112) values.
   - Replace `f"foreman:fix-attempt-{attempt}"` (line 483) with
     `Labels.fix_attempt(attempt)`.
   - Replace `.startswith("foreman:fix-attempt-")` predicates (lines
     600 + 638) with `.startswith(Labels.FIX_ATTEMPT_PREFIX)`.
   - DELETE the now-unused private `_LABEL_*` constants.
9. Create `packages/foreman/tests/test_labels_keystone.py` with the
   three tests:
   - `test_every_foreman_label_literal_in_v3_catalog_matches_labels_class`
   - `test_every_label_in_catalog_is_referenced_by_a_live_consumer`
   - `test_init_label_catalog_covers_labels_all`
   Plus the `_V3_CATALOG_FILES` and `_V2_DEPRECATED_FILES` module-
   private tuples. Test module docstring references issue #194 and
   the v2-exclusion rationale.
10. Run `just check`. Resolve any lint / typecheck / test regression
    inline; do not skip or `# noqa` past a real failure.
11. Run the verification grep from the issue body (with the v2-file
    exclusions noted in step 7 of the Worker procedure above). Confirm
    it returns 0.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/labels.py` | **NEW.** Frozen `Labels` class + classmethods. Leaf module, no internal imports. ~80 lines including docstrings. |
| `packages/foreman/src/foreman/init.py` | Add `from foreman.labels import Labels`. Extract color+description into `_LABEL_METADATA` dict; rebuild `_FOREMAN_LABELS` as a derived list driven by `Labels.all()`. |
| `packages/foreman/src/foreman/reconciler/rules.py` | Import `Labels`. Replace every `"foreman:..."` literal (~25 sites) with `Labels.<UPPER>` or `Labels.IMPL_ATTEMPT_PREFIX` / `Labels.FIX_ATTEMPT_PREFIX`. |
| `packages/foreman/src/foreman/reconciler/actions.py` | Import `Labels`. Replace literals in `_MERGING_LABEL_FOR_TARGET` and the 9 action-branch sites + 2 surface-help sites with `Labels.*`. |
| `packages/foreman/src/foreman/reconciler/observer.py` | Import `Labels`. Replace the inline filter-label list in `_QUERY` with an f-string or builder; ensure resulting query string is byte-identical. |
| `packages/foreman/src/foreman/roles/worker.py` | Import `Labels`. Delete `_LABEL_*` constants (lines 111-116). Switch f-string and `.startswith` callers to `Labels.impl_attempt(...)` and `Labels.IMPL_ATTEMPT_PREFIX`. |
| `packages/foreman/src/foreman/roles/reviewer.py` | Import `Labels`. Delete `_LABEL_*` constants (lines 65-70). Update `_REVIEWER_ENTRY_LABEL_BY_TARGET` map values. |
| `packages/foreman/src/foreman/roles/fixer.py` | Import `Labels`. Delete `_LABEL_*` constants (lines 97-110). Update `_FIXER_ENTRY_LABEL_BY_TARGET` map values. Switch f-string and `.startswith` callers to `Labels.fix_attempt(...)` and `Labels.FIX_ATTEMPT_PREFIX`. |
| `packages/foreman/tests/test_labels_keystone.py` | **NEW.** Three AST-based static-analysis tests + `_V3_CATALOG_FILES` and `_V2_DEPRECATED_FILES` constants. |

No changes to: any file under `packages/foreman/tests/` (per issue:
tests intentionally pin the literal contract); v2-deprecated modules
(`daemon.py`, `dispatcher.py`, `daemon_runners.py`, package-root
`worker.py`, `poller.py`, `queue.py`, `role_dispatch.py`, `locks.py`,
`storage.py`); `foreman/__init__.py` (issue explicitly says don't
export `Labels` from `__init__.py`); colors / descriptions in
`_LABEL_METADATA` (pure refactor); the GraphQL filter contents in
`observer.py` (order + names byte-identical post-refactor).

## Alternatives considered

- **Use a `StrEnum` instead of a frozen class with string attributes.**
  Rejected: `StrEnum` introduces the `.value` footgun
  (`label in str_set` doesn't auto-coerce in every Python version
  Foreman supports, and the code already has both `set[str]` membership
  checks AND f-string interpolation of labels). The plain-class shape
  matches the operator's mental model — `Labels.SPEC_FIX` IS the
  string — without ceremony.
- **Module-level constants in `labels.py` (no class wrapper):
  `LABEL_SPEC_FIX = "foreman:spec-fix"`.** Rejected: would scatter the
  catalog across the module's top-level namespace and make `Labels.all()`
  require module introspection. The class wrapper provides a clean
  namespace AND a natural place for the `all()` / `impl_attempt(n)`
  classmethods. The issue body explicitly recommends the class shape.
- **Refactor the v2 modules too, with a `Labels.Deprecated` sub-namespace
  for v2-only labels.** Rejected: v2 is flagged for removal in a
  separate follow-up PR (per `daemon.py:1-12`'s deprecation note), so
  refactoring it would add churn that gets `git rm`'d shortly. Worse,
  it would either pollute `Labels.all()` with dead state OR require a
  parallel namespace consumers would have to know about — both bad. We
  keep v2 as raw literals + document the exclusion in the keystone test.
- **Skip the keystone test; rely on grep + code review for drift
  detection.** Rejected: the issue body explicitly requires the test
  ("New keystone test `tests/test_labels_keystone.py` that..."), and
  the test cost is one-time. Grep doesn't catch the dead-label case
  (a label in the catalog with no consumer is invisible to a literal
  search). Code review catches the most flagrant drift but misses the
  subtle cases this test is designed for.
- **Define attempt counters as a parameterized function ONLY (no
  IMPL_ATTEMPT_1/2/3 constants).** Rejected: the explicit constants
  match the existing `_FOREMAN_LABELS` shape (init.py:117-122 lists
  six concrete labels), and removing them would force `_LABEL_METADATA`
  to special-case the attempt-counter rows. Cleaner to expose both
  shapes; the keystone test accepts both per the issue body's
  "either is fine" note.

## Open questions

None. The label vocabulary is fixed by `_FOREMAN_LABELS` (line 78-123
of init.py) and the issue body. The keystone-test design is fully
specified. The v2 exclusion is justified by the deprecation note at
`daemon.py:1-12`. The Worker has discretion on a few cosmetic choices
(frozen-dataclass vs. plain class with `__init__ = None`; f-string vs.
builder function for the observer query) — both shapes meet the
acceptance criteria, so the keystone test is the contract.

## Out of scope

- Renaming or recoloring any `foreman:*` label. Pure refactor.
- Introducing a label-versioning scheme (per issue body Out-of-scope).
- Exporting `Labels` from `foreman/__init__.py` (per issue body Out-of-
  scope: "keep the import path explicit").
- Refactoring tests under `packages/foreman/tests/` to use `Labels`
  (per issue body Scope clause: tests pin the literal string contract).
- Refactoring v2-deprecated modules (`daemon.py`, `dispatcher.py`,
  `daemon_runners.py`, package-root `worker.py`, `poller.py`, `queue.py`,
  `role_dispatch.py`, `locks.py`, `storage.py`). Their literals stay
  as-is until the v2 removal PR.
- Updating `docs/architecture/v3-reconciler.md` §3 to mention the
  constants module. The issue Related section flags this as a
  follow-up; a separate docs ticket can handle it once `labels.py`
  has landed.
- Adding a `Labels` `__init__.py` re-export, a CLI subcommand to list
  labels, or any other convenience surface. YAGNI.
- Adding mypy strict-mode coverage for `labels.py` beyond what the
  project's existing `[tool.mypy]` config provides. Out of scope; the
  module is too small to merit project-config churn.
