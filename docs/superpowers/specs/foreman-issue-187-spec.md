# Spec: `create_impl` reattach when local impl branch exists but worktree dir is gone (issue #187)

## Goal

Stop the Worker crash-loop when a stale `foreman/impl-<N>` local branch
survives a wiped impl worktree directory. Today
`WorktreeManager.create_impl` checks only for the worktree DIRECTORY's
existence; if the directory is gone but `git branch` still shows
`foreman/impl-<N>`, the subsequent `git worktree add -b foreman/impl-<N>`
fails with `fatal: a branch named 'foreman/impl-<N>' already exists` and
the role exits non-zero. Make `create_impl` detect that recovery state and
reattach to the existing local branch (Option C in the issue body) so the
prior commit is preserved and downstream `commit_files_to_worktree`
short-circuits via the foreman#117 empty-staged guard. See
[#187](https://github.com/jeffrichley/foreman/issues/187).

## Acceptance criteria

- `WorktreeManager.create_impl`
  (`packages/foreman/src/foreman/worktree.py:228-350`) detects whether
  the local branch `foreman/impl-<N>` exists BEFORE invoking
  `git worktree add`. The detection uses the existing
  `_local_branch_exists(clone_path, impl_branch_name, role_token=...)`
  helper at `worktree.py:663-684` — no new probe helper is introduced.
- Three-arm decision tree, in this order:
  - **(a) Worktree dir present** (`wt_path.exists()` is True) →
    existing idempotent path: recompute `base_branch_for_pr` via
    `_resolve_impl_base_branch` and return the
    `ImplWorktreeResult(path=wt_path, base_branch=base_branch_for_pr)`.
    Unchanged from today.
  - **(b) Worktree dir gone, local branch present** (`wt_path.exists()`
    is False, `_local_branch_exists(...)` is True) → reattach.
    `git worktree add <wt_path> <impl_branch_name>` is invoked WITHOUT
    `-b` so git checks out the existing local branch instead of trying
    to create a new one. `base_branch_for_pr` is still recomputed via
    `_resolve_impl_base_ref_and_branch` (only the second tuple element
    is used) so the impl PR's base reflects current origin state, not
    a cached answer. `_maybe_sync_worktree_deps` runs afterward — the
    wiped worktree has no `.venv` either.
  - **(c) Worktree dir gone, local branch absent** (both `False`) →
    current fresh-create path, unchanged: resolve `(base_ref,
    base_branch_for_pr)` via `_resolve_impl_base_ref_and_branch` then
    invoke `git worktree add -b <impl_branch_name> <wt_path> <base_ref>`.
- The reattach path (b) does NOT run `git fetch origin
  foreman/impl-<N>`. The local branch is the source of truth for any
  commits a prior crashed Worker run produced; refetching could move
  it backward if `origin/foreman/impl-<N>` is stale or absent. This
  diverges from `attach`/`attach_impl` (which DO best-effort fetch),
  by design — those methods exist to onboard a downstream role onto a
  branch the Worker has already pushed; `create_impl` (b) is the
  Worker resuming its own pre-push work.
- A new test
  `test_create_impl_reattaches_when_local_branch_exists_but_worktree_dir_gone`
  in `packages/foreman/tests/test_worktree.py`:
  - seeds a clone via `_seed_clone_with_spec_branch_pushed(clone,
    ticket_id=42)` (so `origin/foreman/issue-42` resolves and the
    stacked path is viable);
  - creates a `foreman/impl-42` local branch directly with
    `git branch foreman/impl-42 origin/foreman/issue-42` (mirrors a
    Worker run that crashed after `git worktree add -b` succeeded
    but before any commit was pushed);
  - does NOT create the `impl-42/` worktree directory (simulates
    `git worktree prune` having cleaned up the registration);
  - calls `mgr.create_impl(clone_path=clone, repo_slug="voice",
    ticket_id=42)` and asserts: no exception raised; the returned
    `ImplWorktreeResult.path` equals `worktrees_root/voice/impl-42`
    and exists on disk; `result.base_branch == "foreman/issue-42"`
    (stacked path probe still wins because `origin/foreman/issue-42`
    is present); the worktree's `git branch --show-current` is
    `foreman/impl-42`; the worktree's `HEAD` SHA equals the
    pre-existing local `foreman/impl-42` tip (proves reattach, not
    re-create).
- A second new test
  `test_create_impl_reattach_preserves_prior_commit_on_local_branch`
  in the same file:
  - seeds via `_seed_clone_with_spec_branch_pushed(...)`;
  - creates `foreman/impl-42` locally with one extra commit on top of
    the spec branch tip (mimics a Worker that committed but crashed
    pre-push);
  - records that commit's SHA;
  - calls `create_impl` and asserts the worktree's HEAD SHA matches
    the recorded prior-commit SHA — i.e., the unpushed commit
    survives the reattach.
- The four existing `create_impl` tests at `test_worktree.py:706-1022`
  (`test_create_impl_creates_dir_with_stacked_branch`,
  `test_create_impl_is_idempotent_on_existing_path`,
  `test_create_impl_separate_from_spec_worktree`,
  `test_create_impl_filters_env_on_git_subprocess_calls`,
  `test_create_impl_falls_back_to_default_when_spec_branch_missing`,
  `test_create_impl_prefers_spec_branch_when_present`,
  `test_create_impl_raises_when_neither_spec_branch_nor_spec_doc_on_default`,
  `test_create_impl_idempotent_returns_result_with_recomputed_base`)
  continue to pass unchanged — these cover arms (a) and (c).
- `WorktreeManager.create_impl`'s docstring (currently at
  `worktree.py:236-295`) is extended with one paragraph naming the
  reattach case, citing issue #187, and noting that the fetch step is
  deliberately skipped on that path (local branch is authoritative).
- The `RuntimeError` raised at `worktree.py:395-402` for the
  "both probes failed" case is unchanged.
- `just check` passes (lint + typecheck + tests).

## Approach

This is a recovery-after-crash bug in the same family as foreman#117
(commit-without-push recovery, fixed in
`packages/foreman/src/foreman/git_hosts/github.py:95-113`) and
foreman#315 (stale dirty worktree, closed). The repro
described in the issue body — `git worktree prune` cleaned up an
orphaned `impl-N/` directory while leaving its local branch
registered — is reachable any time a Worker subprocess is OOM-killed,
SIGKILL'd by a daemon reaper, or otherwise terminated between
`git worktree add -b` (which creates the branch AND the worktree
registration) and the next role-commit / push step. The dogfood
incidents on foreman#138 and foreman#139 (2026-06-07 ~01:00 ET) show
the loss path: Worker crashes, reconciler re-dispatches, `create_impl`
sees no dir but `git worktree add -b` collides with the leftover
branch, exit 255, repeat until `needs-help` escalation. No PR opens
and a cap-cascade-style budget burn occurs.

The fix lives entirely inside `create_impl`. The decision tree the
issue describes (a/b/c) lines up directly with the two existing
predicates we already have:

1. `wt_path.exists()` (filesystem) — already gates today's idempotent
   arm.
2. `_local_branch_exists(clone_path, impl_branch_name,
   role_token=self._role_token)` (git local ref) — already used by
   `attach` (`worktree.py:462`) and `attach_impl`
   (`worktree.py:526`) for the same shape of decision.

Adding a single `elif` between today's idempotent arm and today's
fresh-create arm is enough. The branch in arm (b) is checked out into
the new worktree via `git worktree add <path> <branch>` (no `-b`),
which is the exact invocation pattern `attach` and `attach_impl`
already use successfully for the spec-side worktree.

Why Option C over the alternatives the issue catalogues:

- **Option A (`-B` force-attach)**: silently discards any local
  commits if the prior crashed run had committed but not pushed.
  Defeats foreman#117's whole motivation — that fix exists precisely
  so unpushed work survives.
- **Option B (delete then `-b` fresh)**: needs a divergence check to
  avoid the same data loss. Adding that check duplicates logic that
  Option C avoids by just checking out the existing branch.
- **Option C (reattach via no-`-b` add)**: preserves the local
  branch's commits as-is. The foreman#117 empty-staged-commit
  short-circuit in `commit_files_to_worktree`
  (`git_hosts/github.py:103-113`) handles the "files we're about to
  write match HEAD" case, so the Worker's next commit step does the
  right thing without any new logic. This is the issue body's
  recommendation and the cleanest composition with code we already
  have.

The reattach path deliberately does NOT call `_fetch_origin_branch`
for the impl branch. The semantic difference vs. `attach` / `attach_impl`
is which side is authoritative: `attach`/`attach_impl` exist to onboard
a downstream role onto a branch the Worker already pushed (origin
authoritative), while `create_impl` arm (b) is the Worker resuming its
own pre-push work (local authoritative). Fetching here could surface
a stale origin ref and would also re-set the tracking pointer; neither
is needed because the next push step uses an explicit `<branch>:<branch>`
refspec (`git_hosts/github.py:126`).

`base_branch_for_pr` must still be recomputed on the reattach path.
The local impl branch tip carries pre-push Worker commits, but the
impl PR's base — what `create_pull(base=...)` should be set to — is
strictly a function of current origin state (stacked path if
`origin/foreman/issue-<N>` exists, fallback to default if the spec
doc is on default-only). The cleanest way to share the probe with
the fresh-create arm is to call `_resolve_impl_base_ref_and_branch`
and ignore the first tuple element (the base ref) on the reattach
path, since the reattach path doesn't need to pass any base-ref to
`git worktree add`. We could instead call the idempotent-arm helper
`_resolve_impl_base_branch`, but that helper is just a thin wrapper
over `_resolve_impl_base_ref_and_branch` — calling the latter
directly costs nothing and keeps the new arm's code mirroring the
existing fresh-create arm one statement at a time.

Worker-side composition stays trivial: `run_worker`
(`roles/worker.py:757-762`) already consumes the `ImplWorktreeResult`
opaquely. The reattach path returns the same shape, so no caller
edits are needed and no test in `tests/test_worker_run.py` regresses.

## Sub-requests (topologically sorted)

1. In `packages/foreman/tests/test_worktree.py`, add the failing test
   `test_create_impl_reattaches_when_local_branch_exists_but_worktree_dir_gone`
   following the structure described under Acceptance criteria. Run
   it; confirm it fails with `subprocess.CalledProcessError` whose
   stderr contains `a branch named 'foreman/impl-42' already exists`
   (proves the bug repro is real).
2. In the same file, add the second failing test
   `test_create_impl_reattach_preserves_prior_commit_on_local_branch`.
   Run it; confirm same failure shape.
3. In `packages/foreman/src/foreman/worktree.py`, modify
   `WorktreeManager.create_impl` (lines 228-350). Between today's
   `if wt_path.exists(): ... return ImplWorktreeResult(...)` block
   (lines 308-319) and today's `wt_path.parent.mkdir(...)` line
   (321), insert:
   ```python
   impl_branch_local_exists = _local_branch_exists(
       clone_path, impl_branch_name, role_token=self._role_token
   )
   if impl_branch_local_exists:
       # foreman#187: directory was wiped (e.g., crash + `git
       # worktree prune`) but the local impl branch survives.
       # Reattach without -b so the existing branch's commits
       # are preserved; the foreman#117 empty-staged guard in
       # commit_files_to_worktree handles the "files match HEAD"
       # case on the next commit step.
       wt_path.parent.mkdir(parents=True, exist_ok=True)
       _, base_branch_for_pr = self._resolve_impl_base_ref_and_branch(
           clone_path=clone_path,
           ticket_id=ticket_id,
           spec_branch_name=spec_branch_name,
           default_branch=default_branch,
       )
       subprocess.run(
           [
               "git",
               "worktree",
               "add",
               str(wt_path),
               impl_branch_name,
           ],
           cwd=clone_path,
           check=True,
           capture_output=True,
           text=True,
           env=self._env(),
       )
       _maybe_sync_worktree_deps(wt_path, role_token=self._role_token)
       return ImplWorktreeResult(path=wt_path, base_branch=base_branch_for_pr)
   ```
   Today's remaining fresh-create block (lines 321-350) becomes arm
   (c) and is unchanged.
4. Extend the `create_impl` docstring (today lines 236-295) with one
   paragraph after the existing "Idempotent: if `impl-<N>/` already
   exists..." paragraph: "**Reattach (issue #187)**: if `impl-<N>/`
   is absent but the local branch `foreman/impl-<N>` exists (orphaned
   by a prior worktree-prune after a crash), reattach to it via
   `git worktree add <path> <branch>` (no `-b`) instead of failing
   on the branch-already-exists collision. No fetch of
   `origin/foreman/impl-<N>` runs on this path: the local branch is
   authoritative because it may carry unpushed Worker commits."
5. Re-run the two new tests from steps 1-2; confirm they now pass.
6. Run the existing `create_impl` test block (`test_worktree.py:706-1022`)
   and the `attach_impl` test
   (`test_worktree.py:1025-1090`); confirm all pass — covers arms
   (a) and (c) plus the read-side counterpart.
7. Run `just check` and confirm green (lint + typecheck + tests).
8. Commit and let the daemon push.

## File-level changes

| Path | Change |
| --- | --- |
| `packages/foreman/src/foreman/worktree.py` | Add the arm-(b) reattach block inside `WorktreeManager.create_impl` between the existing idempotent and fresh-create arms. Reuse the existing `_local_branch_exists` helper and `_resolve_impl_base_ref_and_branch` method. Extend the method docstring with one paragraph describing the reattach case and the "no fetch on this path" rationale. |
| `packages/foreman/tests/test_worktree.py` | Add two new tests in the existing `create_impl` block: `test_create_impl_reattaches_when_local_branch_exists_but_worktree_dir_gone` (asserts no crash + reattach to existing branch tip) and `test_create_impl_reattach_preserves_prior_commit_on_local_branch` (asserts an unpushed pre-crash commit survives). No existing tests are modified. |

No other source files change. `roles/worker.py:757-762` consumes
`create_impl`'s return opaquely and inherits the fix; `git_hosts/
github.py:95-116`'s empty-staged-commit short-circuit composes with
the reattach path without any edits.

## Alternatives considered

- **Option A — `git worktree add -B <impl_branch_name> <path>
  <base_ref>` (force-create-or-reset).** Ruled out: `-B` silently
  resets the local branch to `<base_ref>`, discarding any unpushed
  commits a prior crashed Worker run produced. That breaks the
  foreman#117 unpushed-commit-recovery contract.
- **Option B — detect leftover branch, delete it first, then `-b`
  fresh.** Ruled out: safe only if we add a divergence check
  (compare the local branch tip to `origin/foreman/impl-<N>` /
  `origin/<base>`) before deleting. That duplicates logic Option C
  avoids by reattaching to the existing branch directly. The issue
  body also flags this as the wrong composition with foreman#117.
- **Do nothing in `create_impl`; instead, prune the leftover branch
  from `WorktreeManager.cleanup` so it never lingers.** Ruled out:
  `cleanup` only runs on the success path (`run_worker` end of
  pipeline). The bug's repro is when the Worker is killed before
  `cleanup` ever runs, so adding logic there cannot prevent the
  collision on the next role dispatch.
- **Fix it in the reconciler: treat the
  `CalledProcessError` with stderr containing "a branch named ...
  already exists" as a special case and trigger a `git branch -D`
  before re-dispatching.** Ruled out: leaks `git worktree add`'s
  error string into the reconciler, requires the same divergence
  check as Option B to avoid data loss, and crosses a layer boundary
  Foreman tries to keep clean (the worktree module owns the worktree
  state machine).
- **Add a defensive `git worktree prune` at the top of `create_impl`
  to clean up any stale registrations before deciding.** Ruled out
  as primary fix: the issue's repro is AFTER a `git worktree prune`
  — the directory is gone, but the leftover is the local branch, not
  a stale `.git/worktrees/impl-N/` registration. `prune` doesn't
  delete branches. A future ticket could still add it as
  defense-in-depth against the dual case where the registration AND
  the branch both linger, but that's out of scope here.

## Open questions

(none)

## Out of scope

- Refactoring `create`, `attach`, or `attach_impl` to share more
  branch-state-aware logic with `create_impl`. They have different
  authority semantics (origin vs. local) and merging them risks
  regressing the very distinction this spec is preserving.
- Adding a `git worktree prune` call anywhere in
  `WorktreeManager`. Worth considering separately but unrelated to
  the local-branch-survives case this spec fixes.
- Changing `WorktreeManager.cleanup` to also delete the local impl
  branch on success-path cleanup. Possible defense-in-depth, but
  changes the contract `roles/worker.py` and the daemon's failure
  paths rely on, and could mask legitimate recovery states. Track
  separately if desired.
- Reworking the reconciler's role-failure escalation budget so a
  `create_impl` failure does not crash-loop three times in three
  minutes before needs-help fires. The cap-cascade family
  (foreman#345) is the right home for that conversation.
- Touching the foreman#117 empty-staged-commit guard in
  `packages/foreman/src/foreman/git_hosts/github.py:95-113`. This
  spec composes with it; it does not modify it.
- Adding telemetry / structured-log fields for the reattach event.
  A single `print(...)` line in arm (b) saying "[foreman.worktree]
  reattaching to existing local branch foreman/impl-<N> at
  <path>" is fine and matches the existing module style
  (`worktree.py:644-649`, `worktree.py:655-660`,
  `worktree.py:777`), but a dedicated reattach-counter metric
  is not requested by the issue and not added here.
