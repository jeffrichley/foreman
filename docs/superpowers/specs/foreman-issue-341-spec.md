# Spec: impl PR base should target `dev_base_branch` (default `main`), not the spec branch (issue #341)

## Goal
Stop opening Worker impl PRs whose `base` is the orphaned spec branch
(`foreman/issue-<N>`). By the time the Worker runs in v4,
`SpecReviewState` has already merged the spec PR into `main`, so the
spec doc lives on `main` and the impl PR should target `main` (or the
project's configured `dev_base_branch`) directly. This eliminates the
manual `gh pr edit <impl-pr> --base main && gh pr update-branch <impl-pr>`
recovery the operator had to perform on the
[foreman#337 dogfood](https://github.com/jeffrichley/foreman/issues/337)
before PR #339 would merge correctly. Tracks
[foreman#341](https://github.com/jeffrichley/foreman/issues/341).

## Acceptance criteria
- [ ] `WorktreeManager.create_impl` (in
  `packages/foreman/src/foreman/worktree.py`) gains a
  `dev_base_branch: str | None = None` keyword-only parameter and
  branches the impl worktree from `origin/<dev_base_branch>` when the
  argument is provided, falling back to `origin/<default-branch>` when
  it is `None`. The stacked-PR path (branching from
  `origin/foreman/issue-<N>`) is removed.
- [ ] `ImplWorktreeResult.base_branch` returned by `create_impl` is the
  resolved base branch (`dev_base_branch` or default), never
  `foreman/issue-<N>`.
- [ ] The `_resolve_impl_base_ref_and_branch` / `_resolve_impl_base_branch`
  probe helpers are removed (or simplified down to "resolve the
  configured base + fetch it"), along with the
  `_origin_branch_exists(spec_branch_name)` / `_spec_doc_on_origin_default`
  call sites — they no longer have a decision to make.
- [ ] The current `RuntimeError("Cannot create impl worktree... spec
  doc is not present on origin/<default>")` message is removed; the
  new code does not consult the spec branch's existence at all when
  deciding the impl worktree base. (A best-effort `git fetch` of the
  configured base branch remains, mirroring the existing
  best-effort-fetch discipline in `WorktreeManager.create`.)
- [ ] `foreman.roles.worker._run_worker_core` calls `wt_mgr.create_impl`
  with `dev_base_branch=project.dev_base_branch`, mirroring the
  existing Planner pattern at
  `packages/foreman/src/foreman/roles/planner.py:339`.
- [ ] `foreman.roles.worker._run_worker_core` passes
  `base=wt_result.base_branch` to `_create_pull_with_base_fallback`
  (the existing call shape at `worker.py:1031` stays, but
  `wt_result.base_branch` is now the resolved
  `dev_base_branch`-or-default — never the spec branch).
- [ ] `_create_pull_with_base_fallback` and its 422
  "base invalid" retry helper (`_is_invalid_base_422`) stay in place
  as defense in depth — they remain useful if a project ever
  misconfigures `dev_base_branch` to a name that doesn't exist on
  origin. No semantic change required.
- [ ] A unit test in `packages/foreman/tests/test_worktree.py` (or a
  new sibling test file) asserts
  `wt_mgr.create_impl(..., dev_base_branch=None).base_branch ==
  "main"` against a wired bare-origin fixture WHERE
  `origin/foreman/issue-<N>` exists. The pre-change behavior would
  have returned `"foreman/issue-<N>"`; the post-change behavior must
  return `"main"`. This is the unit test that pins the bug fix at the
  decision point.
- [ ] A second unit test in the same file asserts
  `wt_mgr.create_impl(..., dev_base_branch="develop").base_branch ==
  "develop"` against a fixture with a seeded `origin/develop` branch
  (pin the override path).
- [ ] A unit test in
  `packages/foreman/tests/v4/roles/test_worker_core.py` (new file)
  exercises `_run_worker_core` with mocks for `WorktreeManager`,
  `ProviderFacade.run_agent`, `_run_check_command`,
  `host.push_branch`, `_verify_impl_branch_remote_state`, and a
  `MagicMock` PyGithub `Repository`. It asserts
  `repo.create_pull.call_args.kwargs["base"] == "main"` on a Worker
  run whose `WorkerOutput.outcome == "implemented"`. The mocks return
  `wt_result.base_branch == "main"` so the assertion lands at the
  intended decision point.
- [ ] No new failing tests in `just check` —
  `new_failures_count == 0` per the Worker's pre-push gate.
- [ ] No spec-branch retarget step is needed in the merge runner. The
  existing `daemon_runners.merge_impl_pr` path (and its observers)
  continue to work unchanged because the impl PR is now correctly
  based on `main` from the start.

## Approach
The defect is structural: v4's `WorktreeManager.create_impl` was
written for a "stacked PR" design where the impl PR sat on top of the
spec PR (`base = foreman/issue-<N>`) so the spec could be reviewed +
merged independently. That design pre-dates v4's `SpecReviewState`,
which merges the spec PR into `main` BEFORE transitioning to
`ImplementingState` (see
`packages/foreman/src/foreman/v4/states/spec_review.py:30` — the
`ctx.git.merge_pr(...)` call in `verify()`). By the time the Worker
runs, the spec is already on `main` and the spec branch is either
deleted (auto-delete-on) or sitting at the same commit as `main`
(auto-delete-off — foreman's case). Either way, stacking the impl PR
on the spec branch is no longer correct: the only effect is that
merging the impl PR via the UI/API lands on the orphan spec branch
instead of `main`, exactly the symptom #341 documents on PR #339.

The fix is to teach `WorktreeManager.create_impl` to branch the impl
worktree off `origin/<dev_base_branch>` (or `origin/<default>`) — the
same mechanism `WorktreeManager.create` already uses for spec
worktrees (`worktree.py:252`) — and report that base on
`ImplWorktreeResult.base_branch`. The Worker's existing
`base=wt_result.base_branch` argument to
`_create_pull_with_base_fallback` (`worker.py:1031`) then becomes
correct automatically. The Planner's call site
(`roles/planner.py:339`) is the template: Worker passes
`dev_base_branch=project.dev_base_branch` into `create_impl` the
same way Planner passes it into `create`.

The stacked-PR decision tree (`_resolve_impl_base_ref_and_branch`)
goes away. With it goes the `RuntimeError("Cannot create impl
worktree... spec doc is not present on origin/<default>")` case from
issue #48 — that fallback was the right answer when the only
alternative was the now-defunct stacked path. The new code simply
branches from the configured base; we no longer have to consult the
spec branch's existence.

The 422 "base invalid" fallback in `_create_pull_with_base_fallback`
(`worker.py:491`) stays as a belt-and-suspenders defense for the
remaining failure mode (operator typo in `dev_base_branch`,
upstream branch protection rejecting the configured base, etc.).

**Pattern naming (per `CLAUDE.md` Decision 4):** No GoF pattern fits
— this is straightforward removal of a vestigial design choice from
the pre-v4 era. The Google engineering principle that applies is
"make the right thing easy": after `SpecReviewState` merges the spec,
the obvious thing to do when opening the impl PR is target `main`,
which is also the thing operators expect when they hit "Merge pull
request" in the GitHub UI. Today we make the wrong thing easy (silent
merge into the orphan spec branch); the fix restores the principle.

## Sub-requests (topologically sorted)
1. In `packages/foreman/src/foreman/worktree.py`, edit
   `WorktreeManager.create_impl` to accept
   `dev_base_branch: str | None = None` as a keyword-only argument
   (mirror the existing `WorktreeManager.create` signature at
   `worktree.py:193`). Resolve `base_branch = dev_base_branch or
   _resolve_default_branch(clone_path, role_token=self._role_token)`.
   Fetch it best-effort via
   `_fetch_origin_branch(clone_path, base_branch, role_token=self._role_token)`.
2. Replace the body that calls `_resolve_impl_base_ref_and_branch`
   with a direct branch from `origin/<base_branch>` — i.e., the
   `git worktree add -b foreman/impl-<N> <wt_path>
   origin/<base_branch>` invocation. The
   `self._self_heal_orphaned_branch(...)` call stays as-is right
   before the `worktree add`.
3. Return
   `ImplWorktreeResult(path=wt_path, base_branch=base_branch)` from
   both the fresh-create and the idempotent-existing-path branches.
   The idempotent branch should recompute `base_branch` the same way
   the fresh path does (no probing of the spec branch).
4. Delete `_resolve_impl_base_ref_and_branch` and
   `_resolve_impl_base_branch`. Verify no other call sites exist
   (`grep -r _resolve_impl_base_ref_and_branch packages/`).
5. In `packages/foreman/src/foreman/roles/worker.py`,
   `_run_worker_core` (around `worker.py:828-834`), pass
   `dev_base_branch=project.dev_base_branch` into
   `wt_mgr.create_impl(...)`. No other line in the Worker changes —
   `base=wt_result.base_branch` at `worker.py:1031` is already
   correct because `wt_result.base_branch` now resolves to the right
   value.
6. In `packages/foreman/tests/test_worktree.py`, update the existing
   tests that asserted the stacked-PR or fallback behavior:
   - `test_create_impl_creates_dir_with_stacked_branch` (around
     line 693): rename + rewrite to
     `test_create_impl_branches_from_default_when_dev_base_branch_none`
     and assert `result.base_branch == "main"`. Seed
     `origin/foreman/issue-<N>` to prove that the new code does NOT
     pick it up (pre-change behavior would have).
   - `test_create_impl_falls_back_to_default_when_spec_branch_missing`
     (around line 947): keep the test name OR rename to
     `..._branches_from_default_when_dev_base_branch_none_and_no_spec_branch`
     — either way assert `result.base_branch == "main"`.
   - `test_create_impl_prefers_spec_branch_when_present` (around
     line 988): DELETE this test. The stacked-PR path no longer
     exists; this test now pins a behavior we are intentionally
     removing.
   - `test_create_impl_raises_when_neither_spec_branch_nor_spec_doc_on_default`
     (around line 1029): DELETE this test. The `RuntimeError` path
     it pins is gone.
   - `test_create_impl_idempotent_returns_result_with_recomputed_base`
     (around line 1060): update its expectations to assert
     `base_branch == "main"` in the idempotent return.
7. In `packages/foreman/tests/test_worktree.py`, add a NEW test
   `test_create_impl_branches_from_dev_base_branch_override`:
   - Wire an `origin/develop` branch on the bare upstream fixture.
   - Call `mgr.create_impl(..., dev_base_branch="develop")`.
   - Assert `result.base_branch == "develop"`.
   - Assert the worktree's HEAD commit equals `origin/develop`'s
     HEAD (use `git rev-parse HEAD` from the worktree path).
8. Create `packages/foreman/tests/v4/roles/test_worker_core.py`. Add
   one async test
   `test_worker_opens_impl_pr_with_base_main_not_spec_branch` that:
   - Patches `foreman.roles.worker.WorktreeManager` so its
     `create_impl` returns an
     `ImplWorktreeResult(path=tmp_path, base_branch="main")`.
   - Patches `foreman.roles.worker._run_check_command` to return
     `(0, set(), "")` on both the baseline and the post-Worker call.
   - Patches `foreman.roles.worker._sanitize_head_commit_auto_close`
     to a no-op.
   - Patches `foreman.roles.worker._verify_impl_branch_remote_state`
     to a no-op.
   - Patches `foreman.roles.worker.build_role_resources` to return
     `(mock_host, "fake-token", mock_client)` where `mock_host` has
     a no-op `push_branch` and `mock_client.get_repo(...)` returns
     a `MagicMock` Repository whose `create_pull(...)` is recorded.
   - Patches `provider.run_agent` to return a `WorkerOutput` with
     `outcome="implemented"`, `commits_made=[<one fake>]`,
     `pr_title="feat: x"`, `pr_body="body"`,
     `did_check_pass=True`.
   - Runs `_run_worker_core(...)` and asserts
     `repo.create_pull.call_args.kwargs["base"] == "main"` and
     `repo.create_pull.call_args.kwargs["head"] ==
     "foreman/impl-<N>"`.
   - This is the integration-level pin the issue requests as
     "v4 e2e test asserts the recorded base on a real dogfood run":
     it exercises the full Worker role function from issue-URL to
     create_pull args, with only the actual GitHub + LLM seams
     mocked.
9. Run `just check` from the worktree root. Confirm exit zero and
   `new_failures_count == 0`. If the existing
   `test_create_impl_falls_back_to_default_when_spec_branch_missing`
   test has assertions that conflict with the rewrite in
   sub-request 6 (e.g., on the
   `RuntimeError("Cannot create impl worktree...")` text), update
   the assertions in place rather than leaving stale expectations.
10. Document the behavior change in the impl PR description: cite
   foreman#341, name PR #339 as the historical evidence, and note
   that the Implementation requires no migration on the operator
   side (the next dogfood ticket opens its impl PR directly on
   `main`).

## File-level changes
| File | Change |
|------|--------|
| `packages/foreman/src/foreman/worktree.py` | Add `dev_base_branch` kwarg to `create_impl`; remove stacked-PR + spec-doc-on-default probe path; delete `_resolve_impl_base_ref_and_branch` and `_resolve_impl_base_branch`. |
| `packages/foreman/src/foreman/roles/worker.py` | Pass `dev_base_branch=project.dev_base_branch` into `wt_mgr.create_impl(...)`. No other code changes (the `base=wt_result.base_branch` line is already correct given the upstream change). |
| `packages/foreman/tests/test_worktree.py` | Update three existing `create_impl` tests; delete two (`..._prefers_spec_branch_when_present`, `..._raises_when_neither_spec_branch_nor_spec_doc_on_default`); add one (`..._branches_from_dev_base_branch_override`). |
| `packages/foreman/tests/v4/roles/test_worker_core.py` | NEW. One async test pinning `repo.create_pull.call_args.kwargs["base"] == "main"` against `_run_worker_core` with all GitHub + LLM + worktree seams mocked. |

No changes to: `packages/foreman/src/foreman/branches.py`,
`packages/foreman/src/foreman/v4/states/implementing.py`,
`packages/foreman/src/foreman/v4/config.py` (the
`ProjectConfig.dev_base_branch` field already exists and is the
authoritative source — we simply start reading it on the Worker side).

## Alternatives considered
- **Minimal fix: only change `worker.py:1031` to use
  `project.dev_base_branch or "main"`, leave `create_impl` branching
  from the spec branch as-is.** Rejected. Functionally this works
  because the spec branch is an ancestor of `main` after
  SpecReviewState's merge, so the PR diff and merge target are
  correct in practice. But it leaves a conceptually-inconsistent
  state in the codebase (impl worktree branched from
  `foreman/issue-<N>` but impl PR targets `main`), and it leaves the
  `_resolve_impl_base_ref_and_branch` decision tree intact —
  including the `RuntimeError` path for "spec doc not on default"
  which is now reachable only by a very narrow operator-error shape.
  The issue's Approach point 2 ("Drop any lingering assumption that
  the Worker stacks on top of the spec PR") explicitly rules out
  the minimal-fix shape.
- **Retarget the impl PR's base via `pr.edit(base=main)` after
  creating it on the spec branch.** Rejected. This is the
  operator-workaround shape (`gh pr edit <pr> --base main`); doing
  it in code would still require an extra API call per impl PR and
  doesn't solve the structural problem — the impl worktree would
  still be branched from the spec branch.
- **Auto-delete the spec branch in `SpecReviewState.verify()` after
  the merge, forcing `WorktreeManager.create_impl` to take the
  existing "fallback to default" path.** Rejected. Out of scope per
  the issue body ("Out of scope: Auto-deleting the spec branch after
  SpecReviewState merges — separate concern; GitHub's branch
  protection / auto-delete settings handle this"). It would also
  couple the SpecReview lifecycle to assumptions about the Worker
  worktree's needs, which is the wrong direction.
- **Pass `base` explicitly from `ImplementingState` instead of
  reading it from `ProjectConfig` inside the Worker.** Rejected.
  Adding a `base` kwarg to `RoleDispatchState` for one role's one
  edge case adds API surface without proportionate benefit. The
  Worker already reads the project config (it owns the V4Config
  lookup at `worker.py:679-694`), so reading one more field is
  free.

## Open questions
- None. The state-machine ordering is unambiguous (`SpecReview`
  always precedes `Implementing` per
  `states/spec_review.py:32-43`); the spec branch is provably an
  ancestor of `main` post-merge; and `ProjectConfig.dev_base_branch`
  is already the authoritative source the Planner reads via the
  same pattern.

## Out of scope
- Auto-deleting the spec branch after SpecReviewState merges.
- Touching `MergingState` or `daemon_runners.merge_impl_pr` —
  changing the impl PR's base is sufficient; the merge runner's
  contract is unchanged.
- Handling repos with non-default mainlines beyond the existing
  `dev_base_branch` config (the issue body excludes this).
- Branch-protection rule changes on `main`.
- Updating the Reviewer-on-impl path
  (`packages/foreman/src/foreman/roles/reviewer.py`) — Reviewer
  reads the PR, not its base; no change required.
- Migrating in-flight impl PRs that were opened with the old
  `base=foreman/issue-<N>` shape. Existing PR #339 was already
  recovered manually; future impl PRs will be opened with the new
  shape from the start.
- Adding telemetry for "did we open the impl PR with the right
  base." The unit test at sub-request 8 is the contract; runtime
  telemetry is overscope for a bugfix.
