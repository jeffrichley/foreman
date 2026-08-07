# Spec: load project instructions from the role's checked-out worktree, not the bare mirror (issue #586)

## Goal

Fix the silent failure described in [#586](https://github.com/jeffrichley/foreman/issues/586): all four role dispatchers call `load_project_instructions(Path(project.local_clone_path))`, but `local_clone_path` is a **bare mirror** (no working tree). Bare mirrors hold git objects only — no file is ever on disk — so the loader always returns `None`, silently discarding every project's committed `.foreman/INSTRUCTIONS.md`.

The fix is to pass `wt_path` (the role's checked-out linked worktree, which IS on disk) instead. The loader function itself is correct; only the four call sites are wrong.

## Acceptance criteria

- After the fix, calling `load_project_instructions` in the Planner, Reviewer, Fixer, and Worker passes the checked-out worktree path (`wt_path`) rather than `Path(project.local_clone_path)` (the bare mirror).
- A project whose `.foreman/INSTRUCTIONS.md` is committed on `main` (or any branch that `wt_path` is based on) has its instructions included in the role's user prompt.
- A project that has never created `.foreman/INSTRUCTIONS.md` continues to produce `None` (no regression in the "instructions optional" contract).
- The `load_project_instructions` docstring is updated to explicitly state that the argument must be a checked-out working tree path, not the bare mirror base.
- `test_prompt_injection.py` contains at least one test per role (planner, reviewer, fixer) asserting that `load_project_instructions` is invoked with `wt_path`, not `Path(project.local_clone_path)`.
- `test_worker_core.py` contains at least one analogous assertion for the worker.
- `just check` exits zero.
- No existing tests regress.

## Approach

**Pattern (Decision 4):** No GoF pattern fits. This is a straightforward bug fix — a wrong argument at four call sites. The applicable Google engineering principle is **"make the right thing easy"**: the loader API was designed for working-tree paths, and the fix aligns the call sites with that design.

### Root cause

`project.local_clone_path` is the daemon's shared base clone. As documented in `packages/foreman/src/foreman/worktree.py:ensure_base_mirror`, this is created with `git clone --mirror` — a bare mirror that holds all git objects but no checked-out working tree. Therefore `load_project_instructions` can never find `.foreman/INSTRUCTIONS.md` on disk there, even when the file is committed and pushed.

### The fix

All four roles already create a checked-out worktree (`wt_path`) **before** calling `load_project_instructions`. The fix is to pass `wt_path` instead of `Path(project.local_clone_path)` at each of the four call sites:

| Role | File | Line | Old argument | New argument |
|---|---|---|---|---|
| Planner | `roles/planner.py` | 347 | `Path(project.local_clone_path)` | `wt_path` |
| Reviewer | `roles/reviewer.py` | 533 | `Path(project.local_clone_path)` | `wt_path` |
| Fixer | `roles/fixer.py` | 615 | `Path(project.local_clone_path)` | `wt_path` |
| Worker | `roles/worker.py` | 1096 | `Path(project.local_clone_path)` | `wt_path` |

`wt_path` is a `Path` (returned by `WorktreeManager.create`, `.attach`, `.attach_impl`, or set from `wt_result.path`) at each call site — no wrapping with `Path(...)` needed.

### Why worktree beats git-show

The issue proposes two alternatives. Reading from the worktree (this spec) is preferable to `git -C <bare> show HEAD:.foreman/INSTRUCTIONS.md` because:
1. No subprocess spawn or error-handling surface added to the loader.
2. The loader's filesystem-read logic is already correct and tested.
3. The worktree is always checked out at the call site, by construction of the role flow.
4. Instructions from the worktree branch (spec or impl branch, based on main) are semantically correct: they reflect the committed state of the file on the relevant branch.

### The "absent vs. unreachable" distinction

After this fix, the false-silent case is eliminated by construction: if the file is committed on the branch the worktree represents, it is on disk; if it is not committed, it is not on disk. The loader's existing "return None on missing file" behavior remains correct — absent genuinely means absent, not "committed but unreachable via a bare mirror."

### Docstring update

`load_project_instructions` in `packages/foreman/src/foreman/instructions.py` currently says:

> `clone_path: Path to the project's local clone (the directory the role dispatcher's worktree is branched from).`

This is exactly the bare-mirror description, and led to the bug. The docstring must be updated to:
- State that the argument must be a **checked-out worktree path**, not the bare mirror base.
- Explain why: the bare mirror has no working tree so files are never present on disk there.
- Name `wt_path` as the canonical source (returned by `WorktreeManager.create` / `attach` / `attach_impl`).

### Test additions

The four role tests should gain regression assertions. The cheapest approach that doesn't disrupt existing test logic: in at least one test per role, change the `patch("foreman.roles.<role>.load_project_instructions", return_value=None)` to use a `MagicMock(return_value=None)` captured in a local variable, then assert `mock_load.call_args[0][0] == wt_path` after the role function returns. This proves the argument passed is the worktree path and not the bare mirror path.

## Sub-requests (topologically sorted)

1. **Update `instructions.py` docstring.** In `packages/foreman/src/foreman/instructions.py`, update the docstring of `load_project_instructions` to replace the description of `clone_path` with a clear statement that the argument must be a **checked-out worktree path**, explain that passing a bare mirror path always returns `None` even when the file is committed (since bare mirrors have no working tree), and direct callers to use `wt_path` from `WorktreeManager.create` / `attach` / `attach_impl`. Do NOT rename the parameter in the function signature (callers use positional form; renaming is scope creep) — the fix is documentation and call-site correction only.

2. **Fix `roles/planner.py` call site.** At line 347, change:
   ```python
   instructions = load_project_instructions(Path(project.local_clone_path))
   ```
   to:
   ```python
   instructions = load_project_instructions(wt_path)
   ```
   `wt_path` is already a `Path` (returned by `wt_mgr.create(...)` at line 333); no `Path(...)` wrapping needed.

3. **Fix `roles/reviewer.py` call site.** At line 533, change:
   ```python
   instructions = load_project_instructions(Path(project.local_clone_path))
   ```
   to:
   ```python
   instructions = load_project_instructions(wt_path)
   ```
   `wt_path` is already a `Path` (returned by `wt_mgr.attach(...)` or `wt_mgr.attach_impl(...)` at lines 515/522).

4. **Fix `roles/fixer.py` call site.** At line 615, change:
   ```python
   instructions = load_project_instructions(Path(project.local_clone_path))
   ```
   to:
   ```python
   instructions = load_project_instructions(wt_path)
   ```
   `wt_path` is already a `Path` (returned by `wt_mgr.attach(...)` at line 594).

5. **Fix `roles/worker.py` call site.** At line 1096, change:
   ```python
   instructions = load_project_instructions(Path(project.local_clone_path))
   ```
   to:
   ```python
   instructions = load_project_instructions(wt_path)
   ```
   `wt_path` is already a `Path` (set at line 967 from `wt_result.path`).

6. **Add regression tests to `test_prompt_injection.py`.** For the planner, reviewer (spec_pr path), and fixer tests, add one new test each (or extend one existing test per role) that captures the `load_project_instructions` mock as a `MagicMock(return_value=None)`, passes it via `patch(...)`, and after the role call asserts:
   ```python
   assert mock_load.call_args[0][0] == wt_path
   ```
   The `wt_path` value is already computed in the test scaffolding (e.g. `wt_path = worktrees_root / "myrepo" / "issue-335"` set as `mock_wt_mgr.create.return_value`). The three new tests need be no larger than the existing tests in scope.

7. **Add regression test to `test_worker_core.py`.** Add one new test (or extend one existing test) that captures the `load_project_instructions` mock and asserts it is called with `wt_path` (i.e., the path returned by the mocked `wt_mgr.create_impl().path`), not `Path(project.local_clone_path)`.

8. **Run `just check`** and verify exit zero.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/instructions.py` | Update `load_project_instructions` docstring: parameter description now explicitly states "checked-out worktree path, not the bare mirror base." |
| `packages/foreman/src/foreman/roles/planner.py` | Line 347: `load_project_instructions(Path(project.local_clone_path))` → `load_project_instructions(wt_path)`. |
| `packages/foreman/src/foreman/roles/reviewer.py` | Line 533: same substitution. |
| `packages/foreman/src/foreman/roles/fixer.py` | Line 615: same substitution. |
| `packages/foreman/src/foreman/roles/worker.py` | Line 1096: same substitution. |
| `packages/foreman/tests/v4/roles/test_prompt_injection.py` | Add one regression test per role (planner, reviewer, fixer) asserting `load_project_instructions` call arg is `wt_path`. |
| `packages/foreman/tests/v4/roles/test_worker_core.py` | Add one regression test asserting `load_project_instructions` call arg is `wt_path` for the worker. |

## Alternatives considered

1. **`git show` from the bare mirror** — `git -C <local_clone_path> show HEAD:.foreman/INSTRUCTIONS.md`. Reads committed content without a working tree. Rejected: adds a subprocess spawn and error-handling surface to the loader, introduces a shell-out dependency that the loader currently lacks, and complicates the test surface. The worktree path approach is strictly simpler and avoids touching `instructions.py` logic at all.

2. **Read from the base clone via `git cat-file`** — same subprocess approach as above but at a lower git layer. Rejected for the same reasons.

3. **Warn/log when `clone_path` is a bare mirror** — detect `(clone_path / "HEAD").exists() and not (clone_path / ".git").exists()` and log a warning. Rejected: this approach treats the symptom (silence) rather than the cause (wrong path). The docstring fix + call-site fix eliminates the bug entirely; adding a warning on top would be redundant and would leave the wrong argument path in place.

4. **Do nothing** — the loader contract says "missing = None"; technically the callers behave correctly given their (wrong) input. Rejected: the operator has committed instructions that silently have no effect, contradicting the documented promise ("The 4 role bots read this file on every role invocation").

## Open questions

*(None — the root cause is certain, the fix is unambiguous, and both worktree-path availability at each call site and the bare-mirror nature of `local_clone_path` are verified in the source.)*

## Out of scope

- Renaming the `clone_path` parameter of `load_project_instructions` (callers use positional form; a rename is a cosmetic API change with no behavior impact — do not do it in this PR).
- Changes to `ensure_base_mirror`, the bare-mirror clone setup, or any other v4 daemon infrastructure.
- Adding a runtime assertion or warning when `clone_path` is a bare mirror — the docstring update + call-site fix is sufficient; defensive runtime detection would be noise.
- Addressing issue #585 (`foreman init` bare-mirror validation) — same root, different symptom, separate spec.
- Changing how `foreman init` writes or validates the instructions file — it already writes to a proper working clone path; this is out of scope.
