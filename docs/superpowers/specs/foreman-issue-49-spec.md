# Spec: unify impl-branch naming convention (issue #49)

## Goal

Fix the impl-branch naming mismatch between the Worker (which creates
the impl branch + PR as `foreman/impl-<N>`) and `daemon_runners` (which
looks up the impl PR as `foreman/issue-<N>-impl`). The mismatch silently
breaks the autonomous loop the moment `ProjectConfig.auto_merge_impl=True`
fires, because the daemon's branch-lookup string never matches what the
Worker actually pushed. Unify on the Worker's convention
(`foreman/impl-<N>`) and centralize both branch names in a new
`foreman.branches` module so this can't drift again.

Tracks issue [#49](https://github.com/jeffrichley/foreman/issues/49).

## Acceptance criteria

- `packages/foreman/src/foreman/branches.py` exists and exports two
  pure functions:
  - `spec_branch(issue_number: int) -> str` returning
    `f"foreman/issue-{issue_number}"`.
  - `impl_branch(issue_number: int) -> str` returning
    `f"foreman/impl-{issue_number}"` (note: `impl-N`, NOT `issue-N-impl`).
- `packages/foreman/src/foreman/daemon_runners.py` imports
  `spec_branch` and `impl_branch` from `foreman.branches` and uses
  them everywhere the previous private `_spec_branch` /
  `_impl_branch` helpers were used (call sites at
  `daemon_runners.py:139`, `:141`, `:192`, `:213`).
- The private `_spec_branch` and `_impl_branch` functions at
  `daemon_runners.py:59-64` are removed (callers now use the shared
  module).
- The module docstring's "Branch naming convention" block at
  `daemon_runners.py:19-22` reads:
  - spec PR head branch: `foreman/issue-N`
  - impl PR head branch: `foreman/impl-N`
- `packages/foreman/src/foreman/worktree.py` imports `spec_branch` /
  `impl_branch` from `foreman.branches` and uses them at the three
  current literal sites: `worktree.py:102` (spec branch in `create`),
  `worktree.py:155-156` (impl + spec branch pair in `create_impl`),
  and `worktree.py:200` (spec branch in `attach`).
- `packages/foreman/src/foreman/roles/worker.py` imports `spec_branch` /
  `impl_branch` from `foreman.branches` and uses them at
  `worker.py:509-510` in place of the two inline f-string literals.
- `packages/foreman/src/foreman/roles/planner.py:170` uses
  `spec_branch(issue_number)` in place of the inline f-string literal.
- `packages/foreman/src/foreman/roles/fixer.py:434` uses
  `spec_branch(issue_number)` in place of the inline f-string literal.
- A new test in `packages/foreman/tests/test_branches.py` asserts:
  - `spec_branch(46) == "foreman/issue-46"`
  - `impl_branch(46) == "foreman/impl-46"`
  - `spec_branch(0)`, `spec_branch(99999)`, and the equivalent
    `impl_branch` cases produce the expected literal strings (cheap
    parametrize covering issue-number variation).
- The existing daemon-runner tests at
  `packages/foreman/tests/test_daemon_runners.py:96-114` and `:159-176`
  are updated so the asserted branch literal is `"foreman/impl-42"`
  (NOT `"foreman/issue-42-impl"`). The comment on
  `test_daemon_runners.py:111` is updated to match
  (`# impl_pr target → looks up PR for branch foreman/impl-42`).
- All other tests under `packages/foreman/tests/` continue to pass
  unchanged — `test_roles_worker.py:533, :1105` and
  `test_worktree.py:655` already assert the correct
  `"foreman/impl-42"` literal and must keep passing without edit.
- `just check` exits zero.

## Approach

The Worker side is canonical: `roles/worker.py:1-5`'s module docstring
explicitly documents `foreman/impl-<N>` as the impl branch convention,
`worktree.py:124-178`'s `create_impl` produces a worktree on that
branch, and the Worker's PR-create call (covered by
`test_roles_worker.py:533`) opens the PR with
`head="foreman/impl-42"`. Three production tests already verify this
convention end-to-end. The bug is local to `daemon_runners.py:63-64`'s
`_impl_branch` returning `f"foreman/issue-{issue_number}-impl"` — a
fourth, incompatible convention that nothing else in the codebase
produces.

The fix is mechanical: replace the wrong string with the right one.
But doing only that leaves the convention duplicated across 8+ literal
f-string sites (3 in `worktree.py`, 2 in `worker.py`, 1 each in
`planner.py`, `fixer.py`, and `daemon_runners.py`). The issue author's
suggestion — extract a `foreman.branches` module so the convention has
one home — directly addresses the *root cause* of the bug (drift
between parallel definitions) at low cost. We follow that suggestion.

The new module is intentionally minimal: two `int -> str` pure
functions, no class, no caching, no side effects. The two functions
mirror the shape of the existing private helpers in
`daemon_runners.py`, just promoted to module-level publics so other
roles can import them. Tests for the module assert the literal
strings, not "calls the helper" — the *value* is the contract.

Test edits are scoped to the two tests in `test_daemon_runners.py`
that encoded the wrong literal:
- `test_run_reviewer_with_impl_target_uses_impl_branch` (line 96)
- `test_merge_impl_pr_merges_and_closes_issue` (line 159)

These tests must keep asserting literal strings (not call
`impl_branch(42)`) — a test that calls the helper to compute its
expected value tests nothing. The literal must be updated to
`"foreman/impl-42"`.

We do NOT change production sites in `worktree.py`, `worker.py`,
`planner.py`, or `fixer.py` semantically — they already produce the
correct branch names. We only refactor their string-construction to
go through the shared helpers so future edits can't reintroduce
drift. Test files that hardcode `f"foreman/issue-{N}"` (e.g.
`test_roles_worker.py:277`, `test_roles_fixer.py:226`,
`test_roles_reviewer.py:191`, `test_worktree.py:612`) are left
unchanged — test files should encode the literal expected value, not
call the helper they're verifying behavior around.

Why centralization is in scope here, not "speculative refactor": the
issue author called it out by name ("`foreman.branches`"), labeled it
"cheap", and the bug literally exists because the convention was
duplicated. Fixing the symptom without fixing the duplication leaves
the failure mode in place for the next time someone re-types one of
the literals slightly wrong.

## Sub-requests (topologically sorted)

1. Create `packages/foreman/src/foreman/branches.py` with the two
   pure helpers `spec_branch(issue_number: int) -> str` returning
   `f"foreman/issue-{issue_number}"` and
   `impl_branch(issue_number: int) -> str` returning
   `f"foreman/impl-{issue_number}"`. Include a short module
   docstring naming this as the single source of truth for Foreman
   branch names.
2. Create `packages/foreman/tests/test_branches.py` with a
   `pytest.mark.parametrize`'d test asserting
   `spec_branch(n) == f"foreman/issue-{n}"` and
   `impl_branch(n) == f"foreman/impl-{n}"` for at least
   `n in (0, 1, 46, 99999)`. The test asserts literal-string
   equality, not helper round-trips.
3. In `packages/foreman/src/foreman/daemon_runners.py`:
   - Replace the module-docstring lines 19-22 with the corrected
     convention block (impl PR head branch: `foreman/impl-N`).
   - Add `from foreman.branches import impl_branch, spec_branch` to
     the imports.
   - Delete the private `_spec_branch` and `_impl_branch` functions
     at lines 59-64.
   - Update the four call sites (`run_reviewer` at lines 139 and 141,
     `merge_spec_pr` at line 192, `merge_impl_pr` at line 213) to
     call the imported `spec_branch` / `impl_branch`.
4. In `packages/foreman/tests/test_daemon_runners.py`:
   - Update line 111's comment to read `# impl_pr target → looks up
     PR for branch foreman/impl-42`.
   - Update line 113's assertion to
     `host.find_pr_for_branch.assert_called_once_with("jeffrichley/voice", "foreman/impl-42")`.
   - Update lines 171-173's assertion to
     `host.find_pr_for_branch.assert_called_once_with("jeffrichley/voice", "foreman/impl-42")`.
5. In `packages/foreman/src/foreman/worktree.py`:
   - Add `from foreman.branches import impl_branch, spec_branch` to
     the imports.
   - Line 102: replace `branch = f"foreman/issue-{ticket_id}"` with
     `branch = spec_branch(ticket_id)`.
   - Line 155: replace `impl_branch = f"foreman/impl-{ticket_id}"`
     with `impl_branch_name = impl_branch(ticket_id)` (rename the
     local to avoid shadowing the import) and update line 167's
     `-b` argument to use `impl_branch_name`.
   - Line 156: replace `spec_branch = f"foreman/issue-{ticket_id}"`
     with `spec_branch_name = spec_branch(ticket_id)` and update
     lines 160 and 169 (`_fetch_origin_branch(clone_path,
     spec_branch_name)` and `f"origin/{spec_branch_name}"`)
     accordingly.
   - Line 200: replace `branch = f"foreman/issue-{ticket_id}"` with
     `branch = spec_branch(ticket_id)`.
6. In `packages/foreman/src/foreman/roles/worker.py`:
   - Add `from foreman.branches import impl_branch, spec_branch` to
     the imports.
   - Lines 509-510: replace the inline f-strings with
     `spec_branch_name = spec_branch(issue_number)` and
     `impl_branch_name = impl_branch(issue_number)` (renamed locals
     to avoid shadowing the imports), then update the rest of the
     function body to reference `spec_branch_name` /
     `impl_branch_name` wherever it currently references
     `spec_branch` / `impl_branch`.
7. In `packages/foreman/src/foreman/roles/planner.py`:
   - Add `from foreman.branches import spec_branch` to the imports.
   - Line 170: replace
     `branch = f"foreman/issue-{issue_number}"` with
     `branch = spec_branch(issue_number)`.
8. In `packages/foreman/src/foreman/roles/fixer.py`:
   - Add `from foreman.branches import spec_branch` to the imports.
   - Line 434: replace
     `branch = f"foreman/issue-{issue_number}"` with
     `branch = spec_branch(issue_number)`.
9. Run `just check` and confirm exit zero.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/branches.py` | NEW. Two pure helpers `spec_branch(n)` / `impl_branch(n)` returning `foreman/issue-N` / `foreman/impl-N`. Single source of truth for branch-name strings. |
| `packages/foreman/tests/test_branches.py` | NEW. Parametrized literal-equality tests for the two helpers across several issue numbers. |
| `packages/foreman/src/foreman/daemon_runners.py` | Fix docstring line 21 (`foreman/issue-N-impl` → `foreman/impl-N`). Delete private `_spec_branch` / `_impl_branch`. Import + use the shared helpers at all four call sites. |
| `packages/foreman/tests/test_daemon_runners.py` | Update two tests + one comment so the asserted literal is `foreman/impl-42` instead of `foreman/issue-42-impl`. |
| `packages/foreman/src/foreman/worktree.py` | Replace three inline branch f-strings with calls to the shared helpers. Rename local vars (`impl_branch` → `impl_branch_name`, `spec_branch` → `spec_branch_name`) where they would otherwise shadow the imports. |
| `packages/foreman/src/foreman/roles/worker.py` | Replace two inline branch f-strings (line 509-510) with calls to the shared helpers. Rename local vars to avoid shadowing. |
| `packages/foreman/src/foreman/roles/planner.py` | Replace one inline branch f-string (line 170) with a call to `spec_branch`. |
| `packages/foreman/src/foreman/roles/fixer.py` | Replace one inline branch f-string (line 434) with a call to `spec_branch`. |

No changes to test files other than `test_daemon_runners.py` and the
new `test_branches.py`. Tests under `test_roles_*.py`, `test_worktree.py`,
and the plan doc at `docs/superpowers/plans/2026-05-30-foreman-walking-skeleton.md`
keep their hardcoded `foreman/issue-N` / `foreman/impl-N` literals —
that is correct for test code (which must encode the expected value
literally, not call the helper it's testing through).

## Alternatives considered

- **Minimal fix: change only `daemon_runners._impl_branch` to return
  `f"foreman/impl-{issue_number}"` and update the two wrong tests.
  Skip the shared module.** Rejected: the bug exists *because* the
  convention is duplicated across 8+ literal sites. A minimal fix
  leaves the drift surface in place; the next time someone retypes
  the literal slightly wrong, the same class of bug recurs. The
  issue author explicitly recommended the shared module and labeled
  it cheap.
- **Adopt the daemon's convention instead — rename the Worker side
  to produce `foreman/issue-N-impl`.** Rejected: would touch
  significantly more code (Worker production paths, the worktree
  manager's `create_impl`, three test files, the e2e setup), AND the
  longer name buys nothing — `foreman/impl-N` and `foreman/issue-N`
  are already visually distinct prefixes. The issue author also
  recommended the shorter form.
- **Extract a richer `foreman.branches` module that also owns the
  worktree path scheme (`<root>/<slug>/issue-N`, `<root>/<slug>/impl-N`)
  and the per-role git identity selection.** Rejected as
  out-of-scope creep: the issue is specifically about branch-name
  string drift between two sites. Worktree paths and git identity
  have their own homes already (`WorktreeManager`, the identity
  resolver) and aren't currently drifting.
- **Add a runtime assertion that the impl PR's head branch name
  matches `_impl_branch()` and emit a clearer error on mismatch.**
  Rejected: this is a workaround disguised as a fix. It would catch
  the mismatch one layer later instead of removing it; it does not
  address the duplication that caused the bug.
- **Do nothing — wait until `auto_merge_impl=True` actually fires in
  production, then fix the breakage reactively.** Rejected: the
  issue is exactly the kind of latent autonomous-loop bug Foreman
  v1's pre-prod phase exists to surface and fix cheaply. Shipping
  the fix now costs ~30 min of Worker time; shipping it after
  enabling the autonomous loop costs an on-call page plus a stalled
  end-to-end run.

## Open questions

(none — the canonical convention is unambiguous from
`roles/worker.py:1-5`'s docstring + the three production tests that
already assert `foreman/impl-N`, the wrong site is isolated to
`daemon_runners.py:63-64`, and the issue author called the shared-module
extraction by name. No judgment calls remain for the Worker.)

## Out of scope

- Renaming the spec-branch convention (`foreman/issue-N`) or the
  worktree path scheme. Only impl-branch naming drifts; spec-branch
  naming is already consistent across all sites.
- Promoting worktree path construction
  (`<worktrees_root>/<slug>/issue-N`, `…/impl-N`) into the new
  `foreman.branches` module. Branch names and filesystem paths have
  different lifecycles; conflating them is the kind of premature
  abstraction the role contract warns against. If a future bug shows
  drift in worktree paths, that's a separate ticket.
- Migrating test files (`test_roles_*.py`, `test_worktree.py`) that
  currently hardcode `f"foreman/issue-{N}"` / `f"foreman/impl-{N}"`
  literals away from the literals. Tests should assert literal
  expected values; routing a test's expected value through the
  helper-under-test defeats the test.
- Adding runtime validation, deprecation warnings, or compatibility
  shims for the old `foreman/issue-N-impl` string. The old string
  was never produced anywhere — no real branch, PR, or worktree on
  disk uses it — so there is nothing to be backward-compatible with.
- Fixing the `auto_merge_impl` config-default question (whether the
  pilot config has `auto_merge_impl=False` today and whether it
  should flip). That's a deployment / pilot-rollout decision, not a
  naming-bug fix.
- Refactoring `daemon_runners.py`'s `_issue_url` / `_pr_url`
  helpers. They aren't drifting and the issue doesn't mention them.
