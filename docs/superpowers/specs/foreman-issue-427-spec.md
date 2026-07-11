# Spec: rebase impl worktree onto current origin/main before Worker check (issue #427)

## Goal

Before the Worker's baseline preflight and LLM dispatch, rebase the impl worktree's branch onto the current `origin/<default-branch>` so any fixes that landed on main after the impl branch was cut are visible during the check. This closes the stale-base re-failure loop where `foreman retry` is defeated by an impl worktree frozen at a pre-fix base. Tracks [foreman#427](https://github.com/jeffrichley/foreman/issues/427).

## Acceptance criteria

- [ ] On every Worker dispatch where the impl branch is local-only (not yet pushed), `WorktreeManager.rebase_impl_onto_origin(...)` is called before `check_command` runs, bringing the impl worktree up to current `origin/<base_branch>`.
- [ ] The rebase is skipped when the impl branch is already on the remote (`origin_branch_exists` returns True). This avoids creating a local/remote divergence that would require a force-push on the BLOCKED-retry path (issue #342).
- [ ] A test asserts: impl branch based on an older main + a fix landed on main afterward → the Worker's check sees the fix (the post-cut commit's file is present in the worktree after `rebase_impl_onto_origin`).
- [ ] A rebase conflict escalates with a distinct, human-readable `EscalationComment` rather than running the check on a broken or half-merged tree. The Worker returns `outcome="incomplete"` early, posts the escalation comment, and logs a stats row — no LLM dispatch happens.
- [ ] The no-op case (impl branch already at `origin/<base_branch>` tip) produces no new commits and no error.
- [ ] `new_failures_count == 0`.

## Approach

**Pattern naming (per CLAUDE.md Decision 4).** No GoF pattern fits cleanly. The Google principle is SRP: `WorktreeManager` owns the git mechanics of preparing the worktree; `_run_worker_core` owns the escalation policy. Each has one reason to change.

**Rebase vs merge.** The issue body names both options. The deciding constraint is whether the branch has been pushed:

- **Local-only branch** (the NeedsHelp-then-retry case: Worker escalated before pushing): `git rebase origin/<base_branch>` is safe — local SHAs are rewritten, no force-push needed, history stays linear.
- **Branch already on the remote** (the BLOCKED-retry case: impl PR was opened, CI is running): skip the rebase entirely. `git rebase` would rewrite SHAs that are already on the remote ref, and we do not force-push in the BLOCKED path. The BLOCKED path already derives its "did CI pass" answer from `existing_impl_pr.mergeable_state`, not from a local `check_command` run, so a stale base is benign there.

The detection of "is this branch on the remote" is a pure-local check: `_origin_branch_exists(clone_path, impl_branch_name)` queries `refs/remotes/origin/<branch>` with `git rev-parse --verify --quiet` — no network, never fails due to transient issues. If it returns `False`, the branch is local-only and rebase is safe.

**Conflict handling.** A rebase conflict means main and the impl branch diverged on the same path(s) in an incompatible way. This is a genuine "needs human / re-plan" signal — not a transient Worker failure. On conflict, `rebase_impl_onto_origin` runs `git rebase --abort` (restoring the worktree to a clean state) then raises `ImplWorktreeRebaseConflictError`. In `_run_worker_core`, this exception is caught inline: a synthetic `WorkerOutput(outcome="incomplete")` with a structured `EscalationComment` is assembled, `post_escalation_comment` is called, `log_worker_run` is called (zero tokens, baseline_failures_count=0, new_failures_count=0), and the function returns a `WorkerRunResult` early — bypassing the LLM dispatch entirely.

**Every dispatch, not just retry.** `rebase_impl_onto_origin` is called on every Worker dispatch after `create_impl`. On a fresh dispatch, `create_impl` bases the new branch on the freshly-fetched `origin/<base_branch>`, so the rebase is a no-op (already up-to-date). On re-dispatch (worktree existed), the rebase picks up any commits that landed on main between the original branch cut and now. The no-op cost is one `git rebase` invocation (fast, local).

**Where in the flow.** The rebase runs after `create_impl` sets up `wt_path`, and before `_read_spec_doc_from_branch` and the baseline preflight. This ensures both the spec-doc read and the baseline `check_command` run see the updated tree.

**Public API surface.** `worktree.py` already exposes public wrappers (`fetch_origin_branch`, `resolve_default_branch`) over private helpers to let other modules avoid importing private names. Following that pattern, `origin_branch_exists(clone_path, branch, *, role_token=None)` is added as a public wrapper over `_origin_branch_exists` so `_run_worker_core` does not need to import a private name.

## Sub-requests (topologically sorted)

1. **`packages/foreman/src/foreman/worktree.py`**: Add `ImplWorktreeRebaseConflictError(Exception)` near the top of the module (before `WorktreeManager`). Add `origin_branch_exists(clone_path, branch, *, role_token=None) -> bool` public wrapper following the pattern of `fetch_origin_branch` and `resolve_default_branch`. Add `WorktreeManager.rebase_impl_onto_origin(*, clone_path, wt_path, base_branch)` method: call `_fetch_origin_branch(clone_path, base_branch, role_token=self._role_token)` first; run `git rebase origin/<base_branch>` in `wt_path` with `check=False` and `self._env()`; if returncode != 0, run `git rebase --abort` in `wt_path` (also `check=False`) then raise `ImplWorktreeRebaseConflictError` with a message that includes `base_branch` and the first 500 characters of stderr.

2. **`packages/foreman/src/foreman/roles/worker.py`**: Import `ImplWorktreeRebaseConflictError` and `origin_branch_exists` from `foreman.worktree`. In `_run_worker_core`, after `wt_path = wt_result.path` and before `spec_doc_content = _read_spec_doc_from_branch(...)`, add the rebase block: if `not origin_branch_exists(Path(project.local_clone_path), impl_branch_name)`, call `wt_mgr.rebase_impl_onto_origin(clone_path=..., wt_path=wt_path, base_branch=wt_result.base_branch)`. On `ImplWorktreeRebaseConflictError`, synthesize `WorkerOutput(outcome="incomplete", ...)` with a populated `EscalationComment`, call `post_escalation_comment` and `log_worker_run` (following the `_on_failure` pattern: `total_sub_requests=0`, `implemented_count=0`, `skipped_count=0`, `skipped_by_reason={}`, `did_check_pass=False`, `confidence="low"`, `pr_number=None`, `baseline_failures_count=0`, `new_failures_count=0`, `duration_seconds=time.monotonic() - start_time`; token/cost/duration_ms/num_turns fields all zero/None), then return a `WorkerRunResult` early with all required fields: `llm_output=<synthesized_output>`, `attempt=attempt`, `pr_url=None`, `final_did_check_pass=False`, and `final_labels=sorted({label.name for label in issue.labels})` (computed inline, since the normal `final_labels` assignment at line 1340 is post-dispatch and not yet in scope).

3. **`packages/foreman/tests/test_worktree.py`**: Add five new tests (all using real git via `_init_git_repo` / `_seed_clone_with_spec_branch_pushed` — matching the file's existing integration-over-subprocess pattern):
   - `test_rebase_impl_onto_origin_is_noop_when_already_current`: create impl worktree from current origin/main; call `rebase_impl_onto_origin`; assert the worktree's HEAD SHA is unchanged and no new commits exist.
   - `test_rebase_impl_onto_origin_picks_up_post_cut_commit`: create impl worktree from T0 on origin/main; add commit T1 to origin/main (push to bare upstream); call `rebase_impl_onto_origin`; assert the new file from T1 is present in the worktree.
   - `test_rebase_impl_onto_origin_raises_on_conflict`: create impl worktree, add conflicting commit on the impl branch and on origin/main (both modify the same file at the same line); call `rebase_impl_onto_origin`; assert `ImplWorktreeRebaseConflictError` is raised.
   - `test_rebase_impl_onto_origin_aborts_cleanly_on_conflict`: same setup as the conflict test; after the exception, verify the worktree has no `REBASE_HEAD` file (rebase was properly aborted — worktree is clean).
   - `test_rebase_impl_onto_origin_uses_env_filter`: monkeypatch `VIRTUAL_ENV=sentinel-do-not-leak`; use `patch("foreman.worktree.subprocess.run", ...)` recording style; assert `VIRTUAL_ENV` not in any captured `env=` kwarg from the rebase subprocess calls.

4. **`packages/foreman/tests/v4/roles/test_worker_core.py`**: Add three new tests following the `test_worker_opens_impl_pr_with_base_main_not_spec_branch` mock pattern (patch WorktreeManager, build_role_resources, _run_check_command, etc.):
   - `test_worker_calls_rebase_before_preflight_when_impl_is_local_only`: patch `foreman.roles.worker.origin_branch_exists` to return `False`; assert `mock_wt_mgr.rebase_impl_onto_origin` was called with `wt_path=impl_wt_path` and `base_branch="main"`.
   - `test_worker_skips_rebase_when_impl_branch_already_on_remote`: patch `foreman.roles.worker.origin_branch_exists` to return `True`; assert `mock_wt_mgr.rebase_impl_onto_origin` was NOT called.
   - `test_worker_escalates_cleanly_on_rebase_conflict`: patch `foreman.roles.worker.origin_branch_exists` to return `False`; configure `mock_wt_mgr.rebase_impl_onto_origin` to raise `ImplWorktreeRebaseConflictError("conflict on path foo.py")`; assert `provider.run_agent` was NOT called; assert the returned `WorkerRunResult.llm_output.outcome == "incomplete"`; assert `log_worker_run` was called with `outcome="incomplete"`, `new_failures_count=0`, `baseline_failures_count=0`; assert `post_escalation_comment` was called.

## File-level changes

| File | Change |
|------|--------|
| `packages/foreman/src/foreman/worktree.py` | Add `ImplWorktreeRebaseConflictError` exception; add `origin_branch_exists(...)` public wrapper; add `WorktreeManager.rebase_impl_onto_origin(...)` method |
| `packages/foreman/src/foreman/roles/worker.py` | Import `ImplWorktreeRebaseConflictError`, `origin_branch_exists`; add rebase block in `_run_worker_core` between `create_impl` and `_read_spec_doc_from_branch` |
| `packages/foreman/tests/test_worktree.py` | Add 5 tests for `rebase_impl_onto_origin` |
| `packages/foreman/tests/v4/roles/test_worker_core.py` | Add 3 tests for the worker's rebase-and-escalate control flow |

## Alternatives considered

1. **Put the rebase inside `create_impl` on the idempotent path.** Rejected: `create_impl`'s idempotent path currently makes no git calls and returns cheaply. Adding a rebase there changes its contract and side-effects for all callers (including tests). The conflict exception would also propagate through the outer `except Exception` handler in `_run_worker_core`, producing a generic NeedsHelp escalation rather than the structured conflict escalation the issue requires.

2. **Always use `git merge --ff-only` instead of `git rebase`.** Merge is safer for branches already on the remote (no SHA rewrite, no force-push needed). But for local-only branches, `git merge --ff-only` produces a merge commit on the impl branch when origin/main has diverged, polluting the impl PR's history. Since we already gate on `origin_branch_exists` to skip the operation for pushed branches, rebase is correct for the local-only case and produces cleaner history.

3. **Always attempt the rebase, including for pushed branches; add a force-push step afterward.** This would also update BLOCKED-retry branches. Rejected: the BLOCKED-retry path in `_run_worker_core` (issue #342) explicitly skips the push+create_pull surface and derives its "did CI pass" answer from `existing_impl_pr.mergeable_state`. A force-push outside that path would overwrite the PR's head ref and potentially confuse CI. Keeping the rebase gated to local-only branches is the minimal-impact fix.

## Open questions

None. The approach is fully grounded in the codebase: `worktree.py` and `worker.py` were read before writing this spec.

## Out of scope

- Refreshing the impl branch for pushed branches (BLOCKED-retry path, issue #342) — that path uses `mergeable_state`, not a local `check_command` run.
- The daemon project-clone refresh (#407 / PR #424) — complementary, already shipped.
- Any change to the spec-side worktree (`foreman/issue-<N>`) base — this fix is exclusively for the impl-side worktree.
- Force-push of a rebased impl branch to the remote — this ticket only covers local rebasing for local-only branches.
