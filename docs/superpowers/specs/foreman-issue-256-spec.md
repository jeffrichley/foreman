# Spec: drop dead `*_failed` outcome Literals from `stats.py` (issue #256)

## Goal

Remove the four legacy `*_failed` values (`spec_failed`, `review_failed`,
`worker_failed`, `fixer_failed`) from the per-role `outcome` Literal unions
in `packages/foreman/src/foreman/stats.py`. PR #255 (commit `b18a683`)
unified every role's exception path on `outcome="exception"`, so these
four values are now dead vocabulary — accepted by the type system but
never emitted by any production code path. This is a type-only cleanup;
no runtime behavior changes. See issue
[#256](https://github.com/jeffrichley/foreman/issues/256).

## Acceptance criteria

- `packages/foreman/src/foreman/stats.py` no longer contains the strings
  `"spec_failed"`, `"review_failed"`, `"worker_failed"`, or
  `"fixer_failed"` in any of the four `log_*_run` `outcome` parameter
  Literal unions. Specifically:
  - `log_fixer_run.outcome` ends as
    `Literal["fixed", "incomplete", "exception"]`.
  - `log_worker_run.outcome` ends as
    `Literal["implemented", "incomplete", "spec_invalid", "exception"]`.
  - `log_planner_run.outcome` ends as
    `Literal["spec_written", "exception"]`.
  - `log_reviewer_run.outcome` ends as
    `Literal["clean", "needs_fix", "exception"]`.
- `grep -nE 'spec_failed|review_failed|worker_failed|fixer_failed' packages/foreman/src/foreman/stats.py`
  returns ZERO hits. (Docstring mentions of the old values are also
  removed; see file-level changes.)
- A new red-first test in `packages/foreman/tests/test_stats.py` —
  `test_log_outcome_literals_exclude_dead_failure_values` — uses
  `typing.get_type_hints(fn)` + `typing.get_args(...)` to assert each of
  the four `log_*_run` functions' `outcome` Literal *excludes* its
  legacy `*_failed` value AND *includes* `"exception"`. The test MUST
  be red against current `main` (where `stats.py` still lists the
  legacy values) and green after the cleanup. This is the contract
  that prevents silent re-introduction of dead vocabulary.
- The existing `test_log_planner_run_accepts_spec_failed_outcome`
  test in `test_stats.py` (lines 319-334) is DELETED. It pins the
  exact vocabulary we are removing, and converting it to a "rejects"
  shape would just duplicate the new typing-based test above (the
  Literal is type-only — `log_planner_run("spec_failed")` does not
  raise at runtime, since the value is passed straight through to
  `CommonEnvelope.outcome: str` which accepts any string).
- `packages/foreman/tests/test_dispatch_recorder.py::test_failure_path_writes_both_ledgers_once_each`
  (lines 342-369) is updated: the literal `outcome="spec_failed"` on
  line 348 becomes `outcome="exception"` (the value production code
  now emits), and the matching assertion on line 369 becomes
  `assert payload["outcome"] == "exception"`. An inline 1-line comment
  records why the test value changed: `# foreman#256: legacy
  "spec_failed" replaced by uniform "exception" (PR #255 / commit
  b18a683)`.
- `packages/foreman/src/foreman/dispatch_recorder.py` is NOT modified.
  Its `outcome: str` parameter typing is intentionally permissive
  (it forwards to `log_*_run` with `# type: ignore[arg-type]`) and
  remains correct after the cleanup.
- `just check` exits zero (lint + mypy + tests). Specifically, `mypy
  packages/foreman/src` continues to pass — every `outcome=` call
  site in `packages/foreman/src/foreman/roles/{planner,reviewer,worker,fixer}.py`
  already emits a value in the trimmed Literal. (Verified pre-spec via
  `grep -n 'outcome=' packages/foreman/src/foreman/roles/` — the only
  literal-typed outcome strings in production code are `"spec_written"`,
  `"implemented"`, `"incomplete"`, `"spec_invalid"`, `"clean"`,
  `"needs_fix"`, `"fixed"`, and `"exception"`.)
- `new_failures_count == 0` against the baseline (no test regressions
  introduced).

## Approach

Three narrow, surgical edits — one production file, two test files.

**1. Trim the four Literal unions in `stats.py`.** The four `outcome`
parameter annotations on `log_fixer_run` (line 193), `log_worker_run`
(line 294), `log_planner_run` (line 411), and `log_reviewer_run`
(line 491) each lose exactly one element. Other allowed values
(`success`-equivalents, `exception`, role-specific values) stay
untouched. The `CommonEnvelope.outcome` field at line 108 stays
`str` — it's the on-disk envelope shape and intentionally permissive
so historical JSONL rows (which DO contain `*_failed` values written
before PR #255) still round-trip through `CommonEnvelope` if read
back. No migration of disk data; only the WRITER contract narrows.

**2. Add the TDD red-first test.** Foreman's `just check` does NOT
run mypy on `packages/foreman/tests/` (verified via the `justfile`
`typecheck` target — `mypy packages/foreman/src` only), so a static
"this no longer type-checks" assertion in test code is invisible to
CI. The mechanism that works in this repo is runtime introspection
of the type hints. Pattern:

```python
from typing import get_args, get_type_hints

from foreman.stats import (
    log_fixer_run,
    log_planner_run,
    log_reviewer_run,
    log_worker_run,
)


def test_log_outcome_literals_exclude_dead_failure_values() -> None:
    """foreman#256: PR #255 (commit b18a683) unified all four role
    runners on ``outcome="exception"``. The legacy ``<role>_failed``
    values are dead vocabulary — accepted by the type system but
    never emitted. This test pins the cleanup so they can't be
    silently re-added."""
    cases = [
        (log_planner_run, "spec_failed"),
        (log_reviewer_run, "review_failed"),
        (log_worker_run, "worker_failed"),
        (log_fixer_run, "fixer_failed"),
    ]
    for fn, dead_value in cases:
        allowed = set(get_args(get_type_hints(fn)["outcome"]))
        assert dead_value not in allowed, (
            f"{fn.__name__}: dead Literal value {dead_value!r} re-introduced"
        )
        # And the replacement is still in the vocabulary.
        assert "exception" in allowed, (
            f"{fn.__name__}: 'exception' missing from outcome Literal"
        )
```

This test is RED against current `main` (each `get_args(...)` set
still contains the dead value) and GREEN after Step 1.

**3. Reconcile the dead-vocab dependencies in existing tests.**

- Delete `test_log_planner_run_accepts_spec_failed_outcome` in
  `test_stats.py:319-334`. It pins the exact value we're removing.
  No equivalent "rejects" form is useful: the Literal isn't validated
  at runtime, so a `pytest.raises(...)` form would be a no-op; the
  typing-introspection form would duplicate the new test in Step 2.
- Update `test_failure_path_writes_both_ledgers_once_each` in
  `test_dispatch_recorder.py:342-369` to pass `outcome="exception"`
  instead of `outcome="spec_failed"`, and update the matching
  assertion. The test is genuinely exercising the failure path; what
  changes is that "failure" is now spelled the same way production
  spells it. One inline comment links the rename back to issue #256
  and PR #255.

**Why this scope and not more.** The issue's references call out
`_NON_FAILURE_OUTCOMES` in `reconciler/exec_log.py` as a separate
Phase A follow-up — explicitly out of scope here. Likewise historical
JSONL lines on disk that still contain `*_failed` values stay
exactly where they are (read-only data; nothing to migrate). The
`CommonEnvelope.outcome: str` envelope field stays permissive so
those lines can still be loaded.

**Conventions followed.** Conventional-commit shape `chore(stats):` for
the impl PR matches `justfile`'s allowed types and `.github/workflows/pr-title-lint.yml`'s
rules (verified against project CLAUDE.md: "fix, chore, docs, refactor,
test, style, build, ci, perf, revert" are all allowed). The Planner
spec PR uses the project-mandated `docs(spec):` scope.

## Sub-requests (topologically sorted)

1. **Write the failing test first.** Add
   `test_log_outcome_literals_exclude_dead_failure_values` to
   `packages/foreman/tests/test_stats.py` (after the existing tests
   in the file, near the bottom). Verify it is RED against unmodified
   `stats.py`:
   ```bash
   uv run --no-sync pytest packages/foreman/tests/test_stats.py::test_log_outcome_literals_exclude_dead_failure_values -v
   ```
   Expected: FAIL with an assertion message like
   `log_planner_run: dead Literal value 'spec_failed' re-introduced`.
2. **Trim the four Literal unions in `packages/foreman/src/foreman/stats.py`:**
   - Line 193: `outcome: Literal["fixed", "incomplete", "fixer_failed", "exception"]`
     → `outcome: Literal["fixed", "incomplete", "exception"]`
   - Line 294: `outcome: Literal["implemented", "incomplete", "spec_invalid", "worker_failed", "exception"]`
     → `outcome: Literal["implemented", "incomplete", "spec_invalid", "exception"]`
   - Line 411: `outcome: Literal["spec_written", "spec_failed", "exception"]`
     → `outcome: Literal["spec_written", "exception"]`
   - Line 491: `outcome: Literal["clean", "needs_fix", "review_failed", "exception"]`
     → `outcome: Literal["clean", "needs_fix", "exception"]`
3. **Prune the matching docstring mentions of the dead values.** The
   four `log_*_run` docstrings currently document each `<role>_failed`
   value (`stats.py` lines ~221-230 for fixer, ~325-333 for worker,
   ~437-440 for planner, ~523-528 for reviewer). Replace each
   `<role>_failed`-shaped description with `"exception"`-shaped wording.
   Sample shape for the planner docstring:
   > `outcome`: `"spec_written"` when the run produced a spec PR;
   > `"exception"` when the role runner caught an uncaught exception
   > before producing structured output (foreman#239 unified across
   > all four roles in PR #255). Required kwarg — foreman#233
   > deliberately does NOT default this.
   Keep the `foreman#23x` issue references in the docstrings; just
   swap the *value* names.
4. **Verify the new test now passes:**
   ```bash
   uv run --no-sync pytest packages/foreman/tests/test_stats.py::test_log_outcome_literals_exclude_dead_failure_values -v
   ```
   Expected: PASS.
5. **Delete `test_log_planner_run_accepts_spec_failed_outcome`** (lines
   319-334 of `test_stats.py`). Verify the remaining `test_stats.py`
   tests still pass:
   ```bash
   uv run --no-sync pytest packages/foreman/tests/test_stats.py -v
   ```
6. **Update `test_dispatch_recorder.py::test_failure_path_writes_both_ledgers_once_each`:**
   - Line 348: change `outcome="spec_failed"` →
     `outcome="exception"`.
   - Line 369: change
     `assert payload["outcome"] == "spec_failed"` →
     `assert payload["outcome"] == "exception"`.
   - Add an inline comment above line 348:
     ```python
     # foreman#256: legacy "spec_failed" replaced by uniform "exception"
     # (PR #255 / commit b18a683); production code no longer emits
     # the legacy value.
     ```
   Verify the test passes:
   ```bash
   uv run --no-sync pytest packages/foreman/tests/test_dispatch_recorder.py::test_failure_path_writes_both_ledgers_once_each -v
   ```
7. **Run the full quality gate:** `just check`. Expected: exit 0.
   `mypy packages/foreman/src` must continue to pass because every
   production call site emits a value still in the trimmed Literal
   (pre-verified via `grep -n 'outcome=' packages/foreman/src/foreman/roles/`).
8. **Stage and commit:**
   ```bash
   git add packages/foreman/src/foreman/stats.py \
           packages/foreman/tests/test_stats.py \
           packages/foreman/tests/test_dispatch_recorder.py
   git commit -m "chore(stats): drop dead *_failed outcome Literals after #229 rename"
   ```

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/stats.py` | Drop `"spec_failed"` (line 411), `"review_failed"` (line 491), `"worker_failed"` (line 294), `"fixer_failed"` (line 193) from the four `outcome` Literal unions. Prune matching `<role>_failed` mentions from each `log_*_run` docstring (replace with `"exception"`-shaped wording, keeping issue refs). `CommonEnvelope.outcome: str` (line 108) is unchanged. |
| `packages/foreman/tests/test_stats.py` | Add `test_log_outcome_literals_exclude_dead_failure_values` (uses `typing.get_args` to pin the four trimmed Literal vocabularies). Delete `test_log_planner_run_accepts_spec_failed_outcome` (lines 319-334) — it pins the exact value being removed. |
| `packages/foreman/tests/test_dispatch_recorder.py` | Update `test_failure_path_writes_both_ledgers_once_each`: `outcome="spec_failed"` → `outcome="exception"` (line 348) and matching assertion (line 369). Add a 1-line inline comment linking the change to issue #256 + PR #255. |

No expected changes to (sanity-checked):

- `packages/foreman/src/foreman/dispatch_recorder.py` — `outcome: str`
  parameter type is intentionally permissive; the forwarding
  `# type: ignore[arg-type]` to `log_*_run` is correct after the
  cleanup because the only production-emitted values are in the
  trimmed Literal.
- `packages/foreman/src/foreman/roles/{planner,reviewer,worker,fixer}.py`
  — verified via grep that every `outcome=...` call site emits a
  value present in the trimmed Literal (`"spec_written"`,
  `"implemented"`, `"incomplete"`, `"spec_invalid"`, `"clean"`,
  `"needs_fix"`, `"fixed"`, `"exception"`).
- `packages/foreman/src/foreman/reconciler/exec_log.py`'s
  `_NON_FAILURE_OUTCOMES` tuple — explicitly out of scope (separate
  Phase A follow-up per issue references).
- Any historical JSONL files under `~/.foreman/stats/` — read-only
  data; nothing to migrate.

## Verification

Before opening the impl PR, the Worker MUST run and record:

1. `grep -nE 'spec_failed|review_failed|worker_failed|fixer_failed' packages/foreman/src/foreman/stats.py`
   — exits non-zero (no matches). Confirms dead vocabulary fully
   removed from production source.
2. `uv run --no-sync pytest packages/foreman/tests/test_stats.py::test_log_outcome_literals_exclude_dead_failure_values -v`
   — PASS. Confirms the new TDD contract is locked in.
3. `uv run --no-sync pytest packages/foreman/tests/test_stats.py packages/foreman/tests/test_dispatch_recorder.py -v`
   — all pass. Confirms the test updates are coherent.
4. `just check` — exit 0. Confirms lint + mypy + full test suite all
   green; specifically confirms `mypy packages/foreman/src` still
   passes (no production call site emits a value outside the
   trimmed Literal).

## Alternatives considered

- **Convert the existing `test_log_planner_run_accepts_spec_failed_outcome`
  into a `test_log_planner_run_rejects_spec_failed_outcome` using
  `pytest.raises(TypeError)`.** Rejected — the Literal is not validated
  at runtime. `log_planner_run` passes its `outcome` argument straight
  to `_envelope_dict(..., outcome=outcome, ...)` which has parameter
  type `outcome: str`, then into a dict that's `json.dumps`'d. Nothing
  raises. The runtime-rejection variant would be a no-op masquerading
  as a test.

- **Use `mypy.api.run()` inside a test to assert the call doesn't
  type-check.** Rejected — invasive (adds `mypy` as a runtime test
  dependency, materially slows the test, couples test outcomes to
  the mypy version installed). `typing.get_type_hints` +
  `typing.get_args` introspection achieves the same contract with
  zero new tooling.

- **Tighten `CommonEnvelope.outcome` from `str` to a Literal of every
  per-role outcome.** Rejected — `CommonEnvelope` is the on-disk
  envelope shape. Historical JSONL rows on disk DO contain the legacy
  `*_failed` values (written before PR #255); narrowing the envelope's
  `outcome` field would break round-trip parsing of those lines. The
  envelope's permissive `str` is the right shape for the read side;
  the WRITER contracts (`log_*_run`) are where we narrow.

- **Also derive `_NON_FAILURE_OUTCOMES` in `reconciler/exec_log.py`
  from the trimmed Literals as part of this PR.** Rejected — the
  issue's References explicitly defer this to a separate Phase A
  follow-up. Bundling it here violates the issue's stated scope and
  introduces a wider blast radius for what is meant to be a narrow
  type-only cleanup.

- **Do nothing — leave the dead values in the Literal forever as
  vestigial vocabulary.** Rejected — the issue explicitly establishes
  that no external consumer of the JSONL schema exists today, so the
  "additive-only Literal for back-compat" constraint that originally
  retained these values is vacuous. Leaving dead vocabulary in a
  type signature is a documented code-quality regression flagged in
  the PR #255 adversarial review (task #367, Tier 2 item T2.3).

## Open questions

(none — the issue is unambiguous, the production call sites have all
been verified to emit only values in the trimmed Literal, and the
test-update strategy follows directly from foreman's quality gate
(`mypy packages/foreman/src` only, not tests).)

## Out of scope

- **Renaming or removing `"exception"` itself.** Out per issue.
- **Adding new outcome values for new failure modes.** Out per issue.
- **The `_NON_FAILURE_OUTCOMES` tuple in
  `packages/foreman/src/foreman/reconciler/exec_log.py`.** Explicitly
  deferred as a separate Phase A item per the issue's out-of-scope
  list.
- **Removing legacy outcome values from historical JSONL files on
  disk.** Read-only data; nothing to migrate.
- **Modifying `CommonEnvelope.outcome: str` to a tighter type.**
  Would break round-trip parsing of historical JSONL rows that
  still contain the legacy values.
- **Modifying `dispatch_recorder.py`'s `outcome: str` parameter
  types.** Intentionally permissive at the fan-out layer; the
  narrowing belongs at the `log_*_run` writers, not at the dispatch
  recorder.
- **Any change to the shape of `success`, `dry_run`, `skipped_capacity`,
  `reset`, `errored:recovery`, or `running` outcomes** (these live
  in different code paths and aren't part of the per-role role-runner
  Literal unions).
- **Renaming or removing the legacy `*_failed`-named tests in any
  file other than `test_stats.py` and `test_dispatch_recorder.py`**
  — grep confirms those two files are the only test sites that
  reference the dead vocabulary (`grep -rnE 'spec_failed|review_failed|worker_failed|fixer_failed' packages/foreman/tests/`
  returned only those two files).
