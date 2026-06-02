# Spec: retarget impl PR base to default branch before auto-merge (issue #62)

## Goal

Fix a silent data-loss bug in `daemon_runners.merge_impl_pr`: it calls
`merge_pull_request` on the impl PR whose `base` is `foreman/issue-<N>`
(the stacked-PR design at `worktree.py:137-149`), and GitHub then squashes
the impl commits onto the spec branch — which `--delete-branch` immediately
deletes — producing an orphan commit that never reaches `main`. The PR is
reported `MERGED`, the work is gone. Before merging an impl PR, retarget
its base to the repo's default branch when the spec PR has already merged.

Tracks issue [#62](https://github.com/jeffrichley/foreman/issues/62).

## Acceptance criteria

- `packages/foreman/src/foreman/daemon_host.py` gains three new methods on
  `GitHubDaemonHost`:
  - `get_pr_base_ref(repo: str, pr_number: int) -> str` returning the PR's
    current `base.ref` (e.g., `"foreman/issue-49"` or `"main"`).
  - `is_pr_merged_for_branch(repo: str, branch: str) -> bool` returning
    True iff GitHub has a closed, merged PR whose head branch matches
    `branch`. Query is scoped to `state="closed"` (not "open") because a
    merged spec PR is closed.
  - `retarget_pr_base(repo: str, pr_number: int, new_base: str) -> None`
    calling PyGithub's `pr.edit(base=new_base)`.
  - `get_default_branch(repo: str) -> str` returning `repo.default_branch`.
- The `_HostLike` protocol in
  `packages/foreman/src/foreman/daemon_runners.py:49-57` is extended to
  include the four new methods so typecheck still passes on the runner.
- `daemon_runners.merge_impl_pr` (lines 204-217) performs this sequence
  before calling `merge_pull_request`:
  1. Look up the impl PR number for `impl_branch(N)` (unchanged).
  2. Read its current `base.ref` via `self._host.get_pr_base_ref(...)`.
  3. If `base.ref == spec_branch(N)` AND
     `self._host.is_pr_merged_for_branch(repo, spec_branch(N))` is True:
     retarget the impl PR's base to `self._host.get_default_branch(repo)`
     via `self._host.retarget_pr_base(repo, impl_pr_number, default_branch)`.
  4. Then call `self._host.merge_pull_request(...)` as today.
- An integration test in
  `packages/foreman/tests/test_daemon_runners.py` named
  `test_merge_impl_pr_retargets_base_when_spec_pr_merged` asserts the
  retarget call happens before the merge call, in that order, using
  `MagicMock`'s `mock_calls` ordering (or a shared `Mock(spec_set=...)`
  parent). The fake `_HostLike`'s
  `is_pr_merged_for_branch` returns `True`, its `get_pr_base_ref` returns
  `"foreman/issue-42"`, and its `get_default_branch` returns `"main"`.
  Assertion: `host.mock_calls` includes
  `call.retarget_pr_base("jeffrichley/voice", 25, "main")` BEFORE
  `call.merge_pull_request("jeffrichley/voice", 25)`.
- An integration test in the same file named
  `test_merge_impl_pr_skips_retarget_when_spec_pr_still_open` asserts
  `retarget_pr_base` is NOT called when `is_pr_merged_for_branch` returns
  `False` (the "spec hasn't landed yet" case). The `merge_pull_request`
  call still happens against the impl PR number — Foreman does not
  short-circuit; the stacked-PR pattern's "spec hasn't merged yet" case
  is preserved verbatim, since aborting would leave the impl PR parked
  with no recovery path.
- An integration test named
  `test_merge_impl_pr_skips_retarget_when_base_already_default` asserts
  that when `get_pr_base_ref` returns `"main"` (i.e., a previous run or
  operator already retargeted), no retarget call is issued and we skip
  straight to `merge_pull_request`. This makes `merge_impl_pr` idempotent
  on retarget — important because the daemon may re-enqueue on crash.
- The existing test `test_merge_impl_pr_merges_and_closes_issue` (line 159)
  is updated to wire the new `_HostLike` methods on its `host` fixture
  (either via the shared `host` fixture at line 39-49 or by setting the
  three new return values in the test). It must still pass without
  asserting retarget call order — its purpose is the merge+close
  behavior, not the retarget logic. Set `get_pr_base_ref` to return
  `"main"` so the retarget branch is skipped.
- The `host` fixture at `test_daemon_runners.py:39-49` adds default
  `MagicMock()` entries for `get_pr_base_ref`, `is_pr_merged_for_branch`,
  `retarget_pr_base`, and `get_default_branch` so existing tests
  inherit safe defaults without breaking.
- Unit tests for the new `daemon_host` methods land in
  `packages/foreman/tests/test_daemon_host.py`:
  - `test_get_pr_base_ref_returns_base_ref` — fakes a PR object with
    `base.ref = "foreman/issue-42"` and asserts the method returns it.
  - `test_is_pr_merged_for_branch_true_when_closed_merged_pr_exists` —
    uses a `_FakePR` with `merged = True` returned via
    `repo.get_pulls(state="closed", head=...)`.
  - `test_is_pr_merged_for_branch_false_when_no_merged_pr` — empty
    iterator from `get_pulls`.
  - `test_is_pr_merged_for_branch_false_when_pr_closed_but_unmerged` —
    PR exists with `merged = False` (closed without merge).
  - `test_retarget_pr_base_calls_pygithub_edit_with_base_arg` — asserts
    `fake_pr.edit` called with `base="main"`.
  - `test_get_default_branch_returns_repo_default_branch` — fakes
    `repo.default_branch = "main"`.
- The `_FakePR` dataclass at `test_daemon_host.py:36-42` gains a
  `base_ref: str = "main"` field exposed as a `base` property
  (`@property def base(self) -> object: return SimpleNamespace(ref=self.base_ref)`),
  AND a `merged: bool = False` field, AND an `edit` method that records
  `base` arg into a `last_edit_kwargs` dict. These extensions are
  additive — existing tests do not need to set the new fields.
- `docs/superpowers/specs/2026-06-01-foreman-daemon-design.md` gets a
  new subsection under §3 ("State machine") OR under the merge-actions
  area, titled "Impl PR base retarget before merge", documenting the
  ghost-merge failure mode discovered 2026-06-02 (foreman#49 → PR #61
  recovery) and the retarget step. Three or four paragraphs is enough.
- `just check` exits zero.

## Approach

The bug's root cause is that `merge_impl_pr` blindly calls
`pr.merge()` regardless of what the PR's `base` branch currently is.
The Worker (per `worktree.create_impl` docstring at
`worktree.py:124-179` and the brief D1 design) deliberately opens the
impl PR with `base=foreman/issue-N` so the spec PR is reviewable and
mergeable independently — that's the stacked-PR pattern. Once the spec
PR merges (manually or via `merge_spec_pr`), the impl PR is left
pointing at a soon-to-be-deleted ref, and GitHub's `merge + delete`
combo creates an orphan commit on the about-to-be-deleted spec branch.

The fix is the **retarget step** the manual walk for foreman#49 skipped
on 2026-06-02. We add it to `merge_impl_pr` so the autonomous loop (and
any future CLI dogfood path that routes through this method) cannot
trip the same wire. The retarget is conditional on two checks, in
order:

1. The impl PR's current `base.ref` is `spec_branch(N)`. If it's
   already the default branch (operator or prior run already
   retargeted), skip — idempotency matters because the daemon
   re-enqueues on crash.
2. The spec PR for `spec_branch(N)` is *merged*. If it's still open
   (rare in the autonomous flow but possible if a human un-merged or
   the pipeline raced), skip — retargeting to `main` and then merging
   would land impl changes that depend on un-landed spec changes,
   producing a broken `main`. The conservative behavior is to merge
   onto the spec branch as today; the impl PR's commits then become
   reachable from the spec branch and will land with the spec PR's
   eventual merge.

Note check #2 is keyed on the SPEC PR's merge state, not the spec
branch's existence on origin. GitHub deletes the spec branch on merge
(if auto-delete-branch is on), but `repo.get_pulls(state="closed",
head=...)` still returns the merged PR's metadata — the head_ref string
matches the original branch name even after the ref is gone.

The four new methods on `GitHubDaemonHost` mirror the shape of the
existing ones (`merge_pull_request`, `find_pr_for_branch`,
`close_issue`): one PyGithub call apiece, thin, no business logic. The
business logic — the two-check conditional — lives in
`merge_impl_pr` where its sibling concerns (PR lookup, label advance,
issue close) already live. This matches the file's existing
"daemon-internal merge actions live here because they share the host
adapter" docstring (line 15-17).

Why we don't take the alternative of "always retarget":
unconditionally retargeting to `main` when the spec PR hasn't merged
would land impl changes on `main` that depend on un-landed spec
changes, breaking the build. The conditional retarget preserves the
stacked-PR pattern's intentional "spec is the integration branch for
the impl" semantics in the rare case the spec hasn't landed yet.

Why we don't extend `git_host.GitHostProvider` (the role-side
abstraction): this is a daemon-internal merge action, not a role
operation. `daemon_host` is where merge sequencing lives. Extending
the role-side facade would force every role to think about retarget
when no role does merges.

Documentation: the daemon design spec at
`docs/superpowers/specs/2026-06-01-foreman-daemon-design.md` needs to
mention this retarget step so the next person reading it understands
why `merge_impl_pr` is more than one line. The doc update is short —
the ghost-merge failure mode + the retarget conditional, no more.

## Sub-requests (topologically sorted)

1. Extend `_FakePR` and add helpers in
   `packages/foreman/tests/test_daemon_host.py`:
   - Add `base_ref: str = "main"` field on `_FakePR` and an `edit`
     method that records `kwargs` (including `base=...`) into
     `self.last_edit_kwargs`.
   - Expose `base` as a property returning a `SimpleNamespace(ref=self.base_ref)`
     (import `from types import SimpleNamespace`).
   - Add `merged: bool = False` field.
2. Add unit tests in `test_daemon_host.py` for the four new methods
   (named per the acceptance-criteria list above). The tests must call
   methods that do not yet exist — they will fail first, then pass
   after step 3.
3. Add the four new methods to `GitHubDaemonHost` in
   `packages/foreman/src/foreman/daemon_host.py`:
   ```python
   def get_pr_base_ref(self, repo: str, pr_number: int) -> str:
       repo_obj = self._gh.get_repo(repo)
       pr = repo_obj.get_pull(pr_number)
       return pr.base.ref

   def is_pr_merged_for_branch(self, repo: str, branch: str) -> bool:
       repo_obj = self._gh.get_repo(repo)
       owner = repo.split("/")[0]
       for pr in repo_obj.get_pulls(
           state="closed", head=f"{owner}:{branch}"
       ):
           if pr.head.ref == branch and pr.merged:
               return True
       return False

   def retarget_pr_base(
       self, repo: str, pr_number: int, new_base: str
   ) -> None:
       repo_obj = self._gh.get_repo(repo)
       pr = repo_obj.get_pull(pr_number)
       pr.edit(base=new_base)

   def get_default_branch(self, repo: str) -> str:
       repo_obj = self._gh.get_repo(repo)
       return repo_obj.default_branch
   ```
4. Run only the new `test_daemon_host` tests to confirm they now pass.
5. Extend the `_HostLike` Protocol in
   `packages/foreman/src/foreman/daemon_runners.py:49-57` with:
   ```python
   def get_pr_base_ref(self, repo: str, pr_number: int) -> str: ...
   def is_pr_merged_for_branch(self, repo: str, branch: str) -> bool: ...
   def retarget_pr_base(
       self, repo: str, pr_number: int, new_base: str
   ) -> None: ...
   def get_default_branch(self, repo: str) -> str: ...
   ```
6. Extend the `host` fixture at `test_daemon_runners.py:39-49` with
   safe `MagicMock(return_value=...)` defaults for the four new
   methods:
   - `get_pr_base_ref`: returns `"main"` (so default tests don't trip
     the retarget branch).
   - `is_pr_merged_for_branch`: returns `False`.
   - `retarget_pr_base`: a plain `MagicMock()`.
   - `get_default_branch`: returns `"main"`.
7. Add the three new tests for `merge_impl_pr` in
   `test_daemon_runners.py` (per the acceptance-criteria list above).
   The retarget-happens test asserts ordering via `host.mock_calls`
   (or `host.method_calls`) e.g.:
   ```python
   call_names = [c[0] for c in host.method_calls]
   assert call_names.index("retarget_pr_base") < call_names.index(
       "merge_pull_request"
   )
   host.retarget_pr_base.assert_called_once_with(
       "jeffrichley/voice", 25, "main"
   )
   ```
8. Update the existing
   `test_merge_impl_pr_merges_and_closes_issue` test if needed so the
   new fixture defaults make it skip retarget — should be no edit
   required if the fixture defaults are wired right; verify and
   adjust only if the assert fails.
9. Modify `daemon_runners.merge_impl_pr` (lines 204-217) to insert the
   retarget logic between the PR-lookup and the merge call:
   ```python
   spec_branch_name = spec_branch(ticket.issue_number)
   current_base = self._host.get_pr_base_ref(project.repo, pr_number)
   if current_base == spec_branch_name and self._host.is_pr_merged_for_branch(
       project.repo, spec_branch_name
   ):
       default_branch = self._host.get_default_branch(project.repo)
       self._host.retarget_pr_base(project.repo, pr_number, default_branch)
   self._host.merge_pull_request(project.repo, pr_number)
   ```
   Add a short comment above the conditional pointing to issue #62 and
   the ghost-merge failure mode so future readers don't strip it as
   "dead code".
10. Update the docstring on
    `daemon_runners.merge_impl_pr` to document the retarget step
    (one or two sentences referencing issue #62).
11. Append a subsection to
    `docs/superpowers/specs/2026-06-01-foreman-daemon-design.md`
    titled "Impl PR base retarget before merge" describing the
    ghost-merge failure mode (foreman#49 → recovery PR #61, caught
    2026-06-02) and the two-check retarget conditional.
12. Run `just check` and confirm exit zero.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/daemon_host.py` | Add four new methods: `get_pr_base_ref`, `is_pr_merged_for_branch`, `retarget_pr_base`, `get_default_branch`. Each is a thin PyGithub wrapper. |
| `packages/foreman/src/foreman/daemon_runners.py` | Extend `_HostLike` Protocol with the four new method signatures. Modify `merge_impl_pr` to retarget the impl PR's base before merging, conditional on (a) current base == spec branch, and (b) spec PR is merged. Update the method's docstring to document the retarget step + issue #62 reference. |
| `packages/foreman/tests/test_daemon_host.py` | Extend `_FakePR` with `base_ref` (exposed via a `base` property using `SimpleNamespace`), `merged` field, and an `edit` method recording kwargs. Add six new unit tests covering the four new daemon_host methods (open vs. closed-merged vs. closed-unmerged vs. absent PR; base.ref readout; edit(base=...) call; default_branch readout). |
| `packages/foreman/tests/test_daemon_runners.py` | Extend the `host` fixture (lines 39-49) with safe defaults for the four new methods. Add three new tests on `merge_impl_pr`: retarget-when-spec-merged (with mock_calls ordering assertion), skip-when-spec-open, skip-when-base-already-default. Verify the existing `test_merge_impl_pr_merges_and_closes_issue` still passes with the new fixture defaults. |
| `docs/superpowers/specs/2026-06-01-foreman-daemon-design.md` | Append a short subsection documenting the impl-PR retarget step, the foreman#49 → PR #61 ghost-merge failure mode that motivated it, and the two-check conditional. |

## Alternatives considered

- **Always retarget the impl PR to the default branch before merge,
  unconditionally.** Rejected: when the spec PR hasn't merged yet (rare
  but possible — auto_merge_spec=False + human delayed merge + impl
  pipeline raced ahead, or a regression in label sequencing), this would
  land impl commits on `main` that depend on un-landed spec changes,
  breaking the build. The conditional preserves the stacked-PR
  pattern's "spec is the integration branch for the impl" semantics
  for the spec-not-yet-merged case.
- **Refuse to merge the impl PR when its base is still the spec branch
  and the spec PR hasn't merged — return an error / raise.** Rejected:
  the issue explicitly asks that "the merge proceeds against the spec
  branch (preserving the stacked-PR pattern's 'spec hasn't landed yet'
  case if that's ever the autonomous shape)." Raising instead would
  leave the impl PR parked with no recovery path inside the daemon's
  loop, demanding human intervention for what is currently a working
  (if unusual) flow.
- **Skip the merged-state check; trigger retarget purely on
  `base.ref == spec_branch(N)`.** Rejected: this collapses to "always
  retarget" with one extra check, hitting the same broken-`main`
  failure mode whenever the spec PR is still open. Two checks are not
  meaningfully more expensive than one PyGithub call.
- **Move the retarget logic to a new dedicated method on
  `GitHubDaemonHost` (`merge_pull_request_with_retarget(repo, pr_number,
  spec_branch)`).** Rejected: the conditional needs to know
  Foreman-specific concepts (the spec branch name for issue N comes
  from `foreman.branches.spec_branch`), which means the method would
  either take a `spec_branch` argument (leaking Foreman semantics into
  the host adapter) or import from `foreman.branches` (a layering
  violation: the host adapter is supposed to be a thin GitHub wrapper).
  Keeping the conditional in `daemon_runners.merge_impl_pr` keeps the
  Foreman-specific logic in the Foreman-specific layer.
- **Add the retarget step to `merge_spec_pr` instead — retarget the
  impl PR's base proactively when we merge the spec PR.** Rejected:
  the impl PR may not exist yet when the spec PR merges (in the normal
  pipeline order: spec merges → Worker runs → opens impl PR → ... →
  impl merges). Even if it did exist, this couples the spec merge
  action to the impl PR's existence in a way that complicates the
  "spec PR can be merged without an impl PR yet" semantics. The right
  layer is `merge_impl_pr`, which always runs after both PRs exist.
- **Adopt a different stacked-PR strategy upstream (e.g., open the
  impl PR with `base=main` from the start; rely on a "merged via the
  spec branch" reviewer-approval pattern instead).** Out of scope for
  this ticket — the issue explicitly says "preserving the stacked-PR
  pattern". Changing the base-at-creation policy would touch the
  Worker, the worktree manager, and the review docs; this is a
  surgical fix to the merge step, not a redesign of the pattern.
- **Do nothing — wait until `auto_merge_impl=True` actually fires in
  production, then fix the breakage reactively.** Rejected: the
  failure mode has already bitten the manual walk on 2026-06-02
  (foreman#49's eight-file PR became an orphan commit; recovery
  required cherry-pick + new PR #61). It will silently bite the
  autonomous flow the moment `auto_merge_impl` flips. The issue is
  exactly the kind of latent autonomous-loop bug Foreman's pre-prod
  phase exists to surface and fix cheaply.

## Open questions

(none — the failure mode is reproduced and documented in the issue body,
the two-check conditional is unambiguous, the four daemon_host helpers
each wrap one PyGithub call, and the test contracts are concrete. The
docstring update on `merge_impl_pr` and the daemon-design doc
subsection are bounded prose tasks.)

## Out of scope

- Exposing the retarget logic in a CLI command (`foreman merge-impl` or
  similar). The issue calls this out under "Adjacent improvements" and
  defers it to a separate ticket. The daemon's `merge_impl_pr` is the
  one entry point that fixes the autonomous-loop bug; the manual CLI
  walk can adopt the same logic in a follow-up when someone routes a
  `merge-impl` subcommand through `DaemonRunners`.
- Updating documentation on the manual walk (e.g., README sections or
  CONTRIBUTING) to call out the retarget step. The issue defers this.
- Changing the default value of `auto_merge_impl` in `ProjectConfig`.
  That is a deployment / pilot-rollout decision, not a bug fix; this
  ticket only ensures the value-of-`True` path is safe.
- Refactoring `merge_spec_pr` to also do any preemptive PR base
  adjustments. The spec PR's base is always `main` (per
  `roles/planner.py` and the spec PR open call); it does not need
  retargeting.
- Adding GitHub Actions / CI integration tests that exercise a real
  PyGithub round-trip. The integration tests stay at the
  `_HostLike` fake level — same discipline as the existing
  `test_daemon_runners.py` suite. Real-GitHub e2e coverage for the
  daemon is its own (existing) test file.
- Adding telemetry, metrics, or audit-log entries specifically for the
  retarget action. The audit log already records `merged_impl_pr`; the
  retarget is a means to that end. If the retarget needs to be
  surfaced explicitly (for debugging the next ghost-merge), that's a
  follow-up.
- Handling the edge case where the spec PR was merged but the impl PR
  was somehow opened with `base != foreman/issue-N` (e.g., a human
  manually retargeted it to a different feature branch). In that case
  the first condition (`current_base == spec_branch_name`) fails, we
  skip retarget, and we merge against whatever base the impl PR
  currently has. The Worker only creates impl PRs with
  `base=foreman/issue-N`, so this edge case requires human
  intervention to produce; if it bites, the operator can intervene
  again.
- Refactoring `_HostLike` into a single shared Protocol module rather
  than duplicating it across files. Out of scope; the issue is about
  the retarget bug, not the protocol layering.
- Defending against GitHub returning multiple PRs for the same head
  branch (legal if branches are reopened/repushed across PRs). The
  `is_pr_merged_for_branch` short-circuits on the first merged match,
  which matches Foreman's reality (one PR per spec branch per ticket).
