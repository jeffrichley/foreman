# Spec: fall back to default branch when spec branch missing in `create_impl` (issue #48)

## Goal

Fix a latent crash in `WorktreeManager.create_impl()`
(`packages/foreman/src/foreman/worktree.py:125-179`): when
`origin/foreman/issue-N` no longer exists — because the spec PR was
merged with `--delete-branch`, GitHub's "Automatically delete head
branches" setting is on, or an operator deleted the branch manually
between Reviewer and Worker — the `git worktree add -b foreman/impl-N
<path> origin/foreman/issue-N` call exits 128 with
`CalledProcessError` and the Worker aborts. Add a fallback that
branches `foreman/impl-N` off `origin/<default-branch>` after
verifying the spec doc actually landed there, and surface the
resolved PR base branch to the Worker so it opens the impl PR with
the correct base.

Tracks issue [#48](https://github.com/jeffrichley/foreman/issues/48).

## Acceptance criteria

- `WorktreeManager.create_impl()` returns a new frozen dataclass
  `ImplWorktreeResult` with fields `path: Path` and `base_branch: str`.
  The `path` semantics match today's `Path` return; `base_branch` is the
  branch name the impl PR should target (either `foreman/issue-N` on
  the stacked-PR path or the repo's default branch on the fallback
  path).
- When `origin/foreman/issue-N` exists (the in-flight pipeline case
  the daemon's plain-merge flow walks today), behavior matches today:
  branch the impl worktree off `origin/foreman/issue-N`, return
  `base_branch="foreman/issue-N"`. All existing `test_create_impl_*`
  tests continue to pass after their `.path` consumption update.
- When `origin/foreman/issue-N` is missing AND `origin/<default-branch>`
  contains `docs/superpowers/specs/foreman-issue-N-spec.md`, branch off
  `origin/<default-branch>` and return `base_branch=<default-branch>`.
  The fallback is reached only when the spec content has demonstrably
  landed on default — never as a silent reach-around for "spec branch
  never existed in the first place".
- When BOTH `origin/foreman/issue-N` is missing AND the spec doc is
  absent from `origin/<default-branch>`, raise a `RuntimeError` whose
  message names the missing spec branch, the default branch checked,
  the expected spec doc path, and references issue #48 so the next
  reader hitting this lands on the design notes. This is strictly
  more actionable than today's deep `subprocess.CalledProcessError`.
- The fallback's default-branch resolution reuses
  `_resolve_default_branch(clone_path)` (worktree.py:288-316) — the
  same helper `create` uses. No second implementation.
- Both the spec-branch fetch and the (fallback-path) default-branch
  fetch go through `_fetch_origin_branch(clone_path, branch)` so
  transient network failures degrade gracefully to the cached origin
  ref (today's contract for `create`).
- The "does `origin/foreman/issue-N` exist?" probe uses `git rev-parse
  --verify --quiet refs/remotes/origin/<branch>` — silent, no network,
  rc 0 iff the ref resolves locally. Not `git ls-remote` (network
  round-trip) and not a branch listing scan.
- The "is the spec doc on the default branch?" probe uses `git
  cat-file -e origin/<default>:docs/superpowers/specs/foreman-issue-N-spec.md`
  — exits zero iff the path exists in that tree. No checkout, no
  working-tree mutation, no network.
- `WorktreeManager.create_impl` stays idempotent on existing worktree
  path: if `<worktrees_root>/<repo_slug>/impl-<N>/` already exists we
  return an `ImplWorktreeResult` for it without re-running git or uv.
  The `base_branch` field on the idempotent return is recomputed from
  current origin state (same probe sequence as a fresh run) — so
  crash-recovery resolves the base from "what origin looks like now",
  not from any cached metadata file. This is documented in the
  docstring so callers don't expect a stable per-worktree base over
  time.
- The Worker (`packages/foreman/src/foreman/roles/worker.py:518-526`
  for the call, `:648-653` for the PR open) is updated to consume the
  new shape: `wt_result = wt_mgr.create_impl(...)`,
  `wt_path = wt_result.path` (kept as a separate binding so the rest
  of the Worker's local variable use is a zero-diff), and
  `base=wt_result.base_branch` (replacing the hard-coded
  `base=spec_branch_name`).
- `uv sync` best-effort hygiene is unchanged: the existing
  `_maybe_sync_worktree_deps(wt_path)` call still runs against the
  resolved worktree path after a successful `git worktree add` on
  either branch of the decision.
- `env=filtered_subprocess_env()` discipline (issue #10) is preserved
  on every new git subprocess introduced by the fallback: the
  `rev-parse --verify`, the `cat-file -e`, and the second
  `_fetch_origin_branch` for the default branch.
- New tests in `packages/foreman/tests/test_worktree.py`:
  - `test_create_impl_falls_back_to_default_when_spec_branch_missing`
    — fixture pushes the spec doc to `origin/main` but does NOT
    create `origin/foreman/issue-42`. Asserts the worktree's
    `HEAD` rev-parse matches `origin/main`'s tip and
    `result.base_branch == "main"` and the worktree branch is
    `foreman/impl-42`.
  - `test_create_impl_prefers_spec_branch_when_present` — fixture
    pushes BOTH `origin/foreman/issue-42` (carrying the spec doc) AND
    `origin/main` (also carrying the spec doc). Asserts
    `result.base_branch == "foreman/issue-42"` and the worktree tip
    matches the spec branch's tip — the stacked path is preferred
    when available.
  - `test_create_impl_raises_when_neither_spec_branch_nor_spec_doc_on_default`
    — origin has neither the spec branch nor the spec doc on
    default. Asserts the raised `RuntimeError`'s message includes
    `"foreman/issue-42"`, the default branch name, the spec doc
    path, and `"#48"`.
  - `test_create_impl_idempotent_returns_result_with_recomputed_base`
    — first call creates `impl-42/` against
    `origin/foreman/issue-42` and returns
    `base_branch="foreman/issue-42"`. The test then deletes
    `foreman/issue-42` from the bare upstream (simulating spec PR
    merge + `--delete-branch` after Foreman's first `create_impl`).
    Second call returns the same `path` AND
    `base_branch=<default>`, recomputed from current origin state.
- The existing `create_impl` tests
  (`test_create_impl_creates_dir_with_stacked_branch`,
  `test_create_impl_is_idempotent_on_existing_path`,
  `test_create_impl_separate_from_spec_worktree`,
  `test_create_impl_filters_env_on_git_subprocess_calls`) are updated
  to consume `.path` off the returned dataclass. Behavior
  assertions are otherwise unchanged.
- A new test in `packages/foreman/tests/test_roles_worker.py`
  patches `WorktreeManager.create_impl` (e.g., via `monkeypatch` on
  `foreman.roles.worker.WorktreeManager.create_impl`) to return an
  `ImplWorktreeResult(path=<tmp>, base_branch="main")` and asserts
  that the recorded `repo.create_pull_calls[0]["base"] == "main"`.
  This locks the Worker's contract on the new dataclass. The test
  reuses the existing `_FakeRepo.create_pull_calls` fixture pattern at
  `test_roles_worker.py:199-216`.
- The docstring on `create_impl` is updated to call out the fallback
  contract: under what condition the fallback path is taken, what
  `base_branch` means for the caller, and that the impl PR's base
  must be `result.base_branch` rather than a hard-coded spec branch.
- The top-of-file Worker docstring at `roles/worker.py:14-16` is
  updated: the bullet describing the `implemented` outcome now reads
  "opens the impl PR via PyGithub with `base=wt_result.base_branch` —
  either the spec branch (D1, stacked PR) or the default branch
  (fallback when the spec branch is gone, issue #48)".
- `just check` exits zero.

## Approach

The current `create_impl` is built around one specific pipeline
shape: the spec PR is open, its branch exists on origin, and the
Worker stacks its impl branch on top of the spec branch (D1 in the
build brief). That shape is correct for the autonomous-daemon flow
because `GitHubDaemonHost.merge_pull_request` (`daemon_host.py:103-115`)
uses GitHub's plain merge-commit strategy without `--delete-branch`,
so the spec branch survives the spec PR merge until garbage collection.

But the same code is reachable from at least three flows that DO
delete the spec branch before the Worker runs:

1. The manual CLI walk
   (`foreman plan → review → merge (with --delete-branch) → implement`)
   — the case this bug was caught on for issue #46 on 2026-06-02.
2. Repos with GitHub's "Automatically delete head branches" setting
   enabled. Foreman doesn't control that switch and shouldn't try to.
3. Any future code change in `daemon_runners.merge_spec_pr` or
   `daemon_host.merge_pull_request` that adopts squash-merge or
   `delete_branch=True`. Each is a one-keyword-argument away from
   becoming a load-bearing default.

In all three cases, `origin/foreman/issue-N` is gone but the spec
doc landed on the default branch as part of the merge. The Worker
has everything it needs — the spec content, a clean base, a stable
branch to PR against — except the worktree manager refuses to
compute it.

The fix is the natural endpoint of the "retarget impl PR base when
spec PR merges" future-ticket the `create_impl` docstring already
mentions (worktree.py:140-142). Rather than retargeting at merge
time (which issue #62 already handles for the case where the impl
branch was created against the spec branch and the spec PR merges
later), we extend `create_impl` itself to choose the right base AT
CREATION TIME when the spec branch is already gone. The impl PR is
then opened with `base=<default>` from the start; no retarget step
is needed because there is no stacking to unwind.

The fallback condition is conservative on purpose: we fall back ONLY
when the spec branch is missing AND the spec doc demonstrably exists
on the default branch. We do NOT fall back when both checks miss —
that case is a genuine "spec was never produced" error, and a loud,
named failure is better than silently branching off an unrelated
default and surprising the reviewer later.

The decision sequence inside `create_impl`:

1. Resolve the default branch via `_resolve_default_branch(clone_path)`
   — same helper `create` uses, same fallback-to-`"main"` semantics.
2. Best-effort `git fetch origin foreman/issue-N` (today's call,
   unchanged). Refreshes the cached origin ref if it exists; warns
   and continues if it doesn't.
3. Probe `git rev-parse --verify --quiet refs/remotes/origin/foreman/issue-N`.
   If rc 0, take the stacked path: `base_ref = "origin/foreman/issue-N"`,
   `base_branch_for_pr = "foreman/issue-N"`.
4. Otherwise the spec branch is gone. Best-effort
   `git fetch origin <default>` to refresh the default ref, then probe
   `git cat-file -e origin/<default>:docs/superpowers/specs/foreman-issue-N-spec.md`.
   If rc 0, take the fallback path: `base_ref = "origin/<default>"`,
   `base_branch_for_pr = "<default>"`.
5. Otherwise both probes failed. Raise `RuntimeError` with a message
   naming the missing spec branch, the default branch checked, the
   expected spec doc path, and a reference to issue #48.
6. Run `git worktree add -b foreman/impl-N <path> <base_ref>`
   (existing call, parameterized on `base_ref`).
7. Run `_maybe_sync_worktree_deps(wt_path)` (unchanged).
8. Return `ImplWorktreeResult(path=wt_path, base_branch=base_branch_for_pr)`.

On the idempotent re-call branch (`wt_path.exists()`), run the same
probe sequence to recompute `base_branch` from current origin state
and return the result without re-running git worktree add or uv
sync.

Why the return type changes shape rather than the Worker re-running
its own probe: the Worker's `repo.create_pull(...)` at
`roles/worker.py:648-653` hard-codes `base=spec_branch_name`. The
natural place for the Worker to learn the correct base is from the
same call that decided where to branch the impl from. Duplicating
the probe in the Worker would just re-derive the same answer from
the same origin state one process-call later, and create a window
for the two answers to disagree if origin moved between the calls.
A two-field frozen dataclass keeps the contract small and the call
sites obviously correlated.

Why we don't query the GitHub PR API for the spec PR's merge state:
the local probe (spec doc exists on `origin/<default>`) is strictly
stronger evidence for "spec content is on the branch we'd be
branching off" than the API call would be. The API tells us the PR
was merged, but doesn't directly answer "what's on the default
branch right now" — a hand-edited PR target, a merged-into-wrong-base
PR, or a spec PR closed without merging by mistake would all fool
the merged-state check while the local probe still gives the right
answer. The local check is also network-free, faster, and works in
test fixtures without needing a live GitHub.

Why `create_impl` is the right layer and not, say, a new method or a
Worker-side branch: `create_impl` already owns the "pick the right
base ref" decision for the impl worktree. Adding a Worker-side
decision would split that responsibility across files in a way that
makes the contract harder to verify. The `attach_impl` counterpart
(used by downstream roles after the Worker pushed the impl branch)
is unaffected — by the time `attach_impl` runs, the impl branch
exists on origin and stands on its own, regardless of which base it
was created from.

Why we don't touch `daemon_runners.merge_impl_pr` retarget logic
from issue #62: that retarget step handles the case where the impl
PR was opened against the spec branch and the spec PR merges later.
It remains correct on both branches of `create_impl`'s new decision:

- If `create_impl` chose `base_branch=foreman/issue-N` (in-flight
  case, spec branch present), the impl PR opens with
  `base=foreman/issue-N`. At merge time, `merge_impl_pr` may
  retarget to default if the spec PR has merged — current behavior,
  no change.
- If `create_impl` chose `base_branch=<default>` (fallback case),
  the impl PR opens with `base=<default>`. At merge time,
  `merge_impl_pr`'s first conditional (`current_base ==
  spec_branch_name`) evaluates false, so no retarget happens.
  Already idempotent.

Why we don't auto-restore the missing spec branch by pushing
`origin/<default>` back to origin as `origin/foreman/issue-N`:
re-creating a branch GitHub treats as deleted creates a phantom
commit history that wasn't there during the spec PR's review, and
worse, `daemon_host.is_pr_merged_for_branch` (`daemon_host.py:124-140`)
would return True for a branch that wasn't the merged one,
silently breaking issue #62's retarget logic. The fallback is the
right shape; resurrection is not.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/worktree.py`, define a new
   `@dataclass(frozen=True)` `ImplWorktreeResult` at module scope
   (just above `class WorktreeManager`) with fields `path: Path`
   and `base_branch: str`. Add a class docstring describing the
   two-branch decision the caller relies on (stacked PR vs.
   fallback to default) and noting that on idempotent re-call the
   `base_branch` is recomputed from current origin state, not
   cached.

2. In `worktree.py`, add a module-level helper
   `_origin_branch_exists(clone_path: Path, branch: str) -> bool`
   that runs `git rev-parse --verify --quiet refs/remotes/origin/<branch>`
   via `subprocess.run(..., check=False, capture_output=True,
   text=True, env=filtered_subprocess_env())` and returns
   `result.returncode == 0`. Sibling shape to `_local_branch_exists`
   at lines 349-358. No prints on either branch.

3. In `worktree.py`, add a module-level helper
   `_spec_doc_on_origin_default(clone_path: Path, default_branch: str,
   ticket_id: int) -> bool` that runs `git cat-file -e
   origin/<default_branch>:docs/superpowers/specs/foreman-issue-<ticket_id>-spec.md`
   with the same `subprocess.run(...)` shape as helper (2). Returns
   True iff rc 0. No prints. The spec doc path matches what the
   Planner writes (`foreman.roles.planner` writes to this path; the
   `_seed_clone_with_spec_branch_pushed` test helper uses the same
   path at `test_worktree.py:617-618`).

4. Modify `WorktreeManager.create_impl` in `worktree.py:125-179`:
   - Change return type annotation from `Path` to
     `ImplWorktreeResult`.
   - Update the docstring to document the fallback condition, the
     error condition, the meaning of `base_branch`, and the
     idempotent-recompute semantics. Reference issue #48.
   - Resolve `default_branch` via `_resolve_default_branch(clone_path)`
     near the top of the method so both probe branches and the
     fresh / idempotent paths can use it.
   - On the idempotent branch (`if wt_path.exists():`), run the
     same probe sequence as the fresh path and return
     `ImplWorktreeResult(path=wt_path, base_branch=...)`.
   - On the fresh path, execute the 8-step decision sequence from
     the Approach section. When both probes fail, raise
     `RuntimeError(f"Cannot create impl worktree for issue
     #{ticket_id}: origin/{spec_branch_name} is missing AND the
     spec doc docs/superpowers/specs/foreman-issue-{ticket_id}-spec.md
     is not present on origin/{default_branch}. The spec PR may
     not have been opened, or it was closed without merging. See
     issue #48 for the design rationale on this fallback path.")`.

5. In `packages/foreman/src/foreman/roles/worker.py`:
   - Update the `create_impl` call at lines 522-526:
     ```python
     wt_result = wt_mgr.create_impl(
         clone_path=Path(project.local_clone_path),
         repo_slug=repo_name,
         ticket_id=issue_number,
     )
     wt_path = wt_result.path
     ```
     Keeping `wt_path` as a separate local binding is deliberate —
     it leaves the ~15 downstream references unchanged.
   - Update the `repo.create_pull(...)` call at lines 648-653:
     replace `base=spec_branch_name` with `base=wt_result.base_branch`.
   - Update the docstring bullet at lines 14-16: read
     "opens the impl PR via PyGithub with `base=wt_result.base_branch`
     — either the spec branch (D1, stacked PR) or the default
     branch (fallback when the spec branch is gone, issue #48)".

6. Add new tests in `packages/foreman/tests/test_worktree.py`
   alongside the existing `create_impl` tests
   (`test_create_impl_*`, starting at line ~635):
   - `test_create_impl_falls_back_to_default_when_spec_branch_missing`
     — fixture writes `docs/superpowers/specs/foreman-issue-42-spec.md`
     on `main`, pushes `main` to origin, does NOT push or create
     `foreman/issue-42`. Calls `mgr.create_impl(...)`. Asserts the
     returned `result.path` exists, `result.base_branch == "main"`,
     the worktree's `git rev-parse HEAD` matches origin/main's tip,
     and the worktree's current branch is `foreman/impl-42`.
   - `test_create_impl_prefers_spec_branch_when_present` — fixture
     creates BOTH `origin/foreman/issue-42` (via the existing
     `_seed_clone_with_spec_branch_pushed` helper) AND has the spec
     doc on `origin/main`. Asserts
     `result.base_branch == "foreman/issue-42"` and the worktree
     tip matches the spec branch's tip (proving the in-flight
     stacked path stays preferred when available).
   - `test_create_impl_raises_when_neither_spec_branch_nor_spec_doc_on_default`
     — fixture has a clean clone + origin with the seed commit on
     `main`, no spec doc anywhere, no spec branch. Asserts
     `RuntimeError` is raised with a message containing
     `"foreman/issue-42"`, `"main"`,
     `"docs/superpowers/specs/foreman-issue-42-spec.md"`, and
     `"#48"`.
   - `test_create_impl_idempotent_returns_result_with_recomputed_base`
     — first call uses `_seed_clone_with_spec_branch_pushed` then
     adds the spec doc to `origin/main` so the fallback path is
     viable. Asserts the first call returns
     `base_branch="foreman/issue-42"`. The test then deletes
     `foreman/issue-42` from the bare upstream
     (`git push origin --delete foreman/issue-42` from the clone)
     and prunes the local origin ref. Second `mgr.create_impl(...)`
     call returns the same `result.path` and
     `result.base_branch == "main"`.
   - Update the four existing `create_impl` tests to read
     `.path` off the returned dataclass:
     `test_create_impl_creates_dir_with_stacked_branch` (lines
     635-668), `test_create_impl_is_idempotent_on_existing_path`
     (lines 671-679), `test_create_impl_separate_from_spec_worktree`
     (lines 682-694), and
     `test_create_impl_filters_env_on_git_subprocess_calls` (lines
     697-727). Each test's existing assertion body is unchanged
     beyond the `.path` access.

7. Add a new test in
   `packages/foreman/tests/test_roles_worker.py` named
   `test_worker_opens_impl_pr_with_base_from_create_impl_result`
   that:
   - Reuses the existing `_FakeRepo` fixture (line ~199) so
     `repo.create_pull_calls` records the kwargs.
   - Monkeypatches `foreman.roles.worker.WorktreeManager.create_impl`
     to return `ImplWorktreeResult(path=<tmp_worktree>, base_branch="main")`.
   - Drives `run_worker` through a happy-path "implemented"
     outcome (mirror the setup of the existing
     `test_run_worker_implemented_*` test at the line
     ~530 assertion).
   - Asserts `repo.create_pull_calls[0]["base"] == "main"`. This
     locks the Worker's contract on the new dataclass.

8. Run `just check` and confirm exit zero. Resolve any new ruff /
   mypy complaints surfaced by the new code (`from foreman.worktree
   import ImplWorktreeResult` import in `roles/worker.py`, the
   return-type annotation change on `create_impl`, the dataclass
   import in `worktree.py`) until clean.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/worktree.py` | Add `ImplWorktreeResult` frozen dataclass with `path` and `base_branch` fields. Add `_origin_branch_exists` and `_spec_doc_on_origin_default` module helpers using `filtered_subprocess_env()`. Modify `WorktreeManager.create_impl` to return `ImplWorktreeResult`: prefer the spec-branch stacked path when `origin/foreman/issue-N` exists, fall back to `origin/<default>` when the spec branch is missing AND the spec doc is on default, raise a clearly-named `RuntimeError` when both probes fail. Idempotent re-call returns a result whose `base_branch` is recomputed from current origin state. Update the docstring with the new contract and an issue #48 reference. |
| `packages/foreman/src/foreman/roles/worker.py` | Update the `create_impl` call to consume `ImplWorktreeResult` (`wt_result = ...; wt_path = wt_result.path`). Replace the hard-coded `base=spec_branch_name` in the impl PR's `create_pull` with `base=wt_result.base_branch`. Update the top-of-file docstring's "implemented" bullet to note the dual-base contract and the issue #48 fallback. |
| `packages/foreman/tests/test_worktree.py` | Add four new tests covering the fallback path, the spec-branch-preferred path, the both-missing error, and idempotency-after-spec-branch-deletion. Update four existing `create_impl` tests to read `.path` off the returned dataclass; behavior assertions otherwise unchanged. |
| `packages/foreman/tests/test_roles_worker.py` | Add one new test that monkeypatches `WorktreeManager.create_impl` to return `ImplWorktreeResult(base_branch="main")` and asserts the Worker's `repo.create_pull` call records `base="main"`, locking the Worker's contract on the dataclass. |

## Alternatives considered

- **Catch `subprocess.CalledProcessError` from the existing
  `git worktree add` and inspect stderr for "couldn't find ...
  origin/foreman/issue-N", then retry with default branch.**
  Rejected: stderr-pattern parsing is brittle across git versions
  and locales. Turning a deep `CalledProcessError` into a high-level
  decision wastes context that's cheaper to compute upfront with
  `git rev-parse --verify --quiet`. The pre-flight probe is also
  silent on success, so it doesn't pollute logs in the common path.
- **Always branch the impl worktree off `origin/<default>` and
  abandon the stacked-PR pattern.** Rejected: the stacked-PR
  pattern is the design contract (D1 in the build brief) and exists
  to keep the spec PR independently reviewable + mergeable.
  Abandoning it would force a redesign of the Reviewer/Worker
  handoff, the daemon's merge sequencing in
  `daemon_runners.merge_impl_pr`, issue #62's retarget logic, and
  the docs that explain why the impl PR's base is what it is. This
  ticket is a fix for one missing-spec-branch case, not a
  pattern-level redesign.
- **Auto-restore the missing spec branch by pushing
  `origin/<default>` back to origin as `origin/foreman/issue-N`
  before branching the impl worktree.** Rejected: re-creating a
  branch GitHub treats as deleted creates a phantom commit history
  that wasn't there during the spec PR's review, and worse,
  `GitHubDaemonHost.is_pr_merged_for_branch` at
  `daemon_host.py:124-140` would then return True for a branch that
  isn't the merged one, silently breaking issue #62's retarget
  logic. The fallback is correct; resurrection isn't.
- **Keep `create_impl` returning `Path` and add a sibling
  `resolve_impl_pr_base(clone_path, ticket_id) -> str` that the
  Worker calls separately.** Rejected: this duplicates the probe
  logic across two methods and opens a window for the two answers
  to disagree if origin state changes between the calls. One
  method, one decision, one return-value shape.
- **Use the GitHub API (PyGithub) to check whether the spec PR was
  merged, rather than checking for the spec doc on
  `origin/<default>` locally.** Rejected: the API answers "was the
  PR merged?"; the local probe answers the stronger question "is
  the spec content on the branch I'd be branching off?". The local
  check is also network-free, faster, and works in test fixtures
  without needing a live GitHub mock.
- **Inject a callback or strategy object into `WorktreeManager` to
  let the Worker customize the fallback policy.** Rejected: there
  is exactly one caller (`run_worker`) and exactly one correct
  policy (prefer spec branch, fall back to default with spec-doc
  proof, raise otherwise). A strategy interface for a fixed two-way
  decision is the wrong shape of complexity.
- **Do nothing — wait for the autonomous flow to adopt
  `--delete-branch` or squash-merge, then fix it then.** Rejected:
  the bug has already bitten the manual CLI walk on issue #46
  (2026-06-02 dogfood), and any operator running on a repo with
  GitHub's "Automatically delete head branches" setting enabled
  hits it the first time the Worker tries to run. The fix is a
  small, well-scoped change to the worktree manager that closes
  the latent risk before it migrates into the daemon.

## Open questions

(none — the decision sequence is straightforward, the probe
commands are well-defined stdlib idioms with deterministic exit
codes, the return-type change has exactly one production caller
(`run_worker`) plus the test suite, and the design preserves issue
#62's retarget logic on both branches of the new decision.)

## Out of scope

- Auto-deleting the impl worktree on PR merge or pipeline
  completion. Cleanup discipline is owned elsewhere; this ticket
  only changes the create path.
- Auto-restoring the spec branch on origin when it's been deleted
  (see Alternatives — actively wrong, not just out of scope).
- Changing the daemon's `merge_spec_pr` to delete the spec branch
  on merge, or to use squash-merge. Adopting either would propagate
  this code path into the autonomous flow; that's a separate config
  decision and a separate ticket.
- Reworking the stacked-PR pattern itself. The fallback adds a
  second branch to the existing decision; it does not replace the
  pattern.
- Touching `WorktreeManager.attach_impl`. That method is read-side:
  by the time it runs, the impl branch exists on origin and stands
  on its own, regardless of whether `create_impl` branched it off
  the spec branch or the default branch.
- A CLI command to manually retarget an open impl PR's base from
  `foreman/issue-N` to `<default>`. That's issue #62's territory
  (`merge_impl_pr` already does the runtime retarget when the spec
  PR is merged); a manual subcommand for the same operation is a
  follow-up.
- Documentation updates beyond the docstrings on `create_impl` and
  the top-of-file Worker docstring. If the architectural spec at
  `docs/superpowers/specs/foreman-v1-architectural-spec.md` should
  call out the fallback path, that's a follow-up doc ticket.
- Telemetry / metrics for "how often was the fallback path taken
  vs. the stacked path?". Useful future-signal but outside this
  ticket. The audit trail in the Worker's JSONL log already captures
  the impl PR's URL, from which the base can be derived if anyone
  wants to back-fill the metric later.
- Defending against the corner case where the spec branch exists on
  origin but its tip is unrelated to the merged spec (e.g., an
  operator pushed an unrelated branch to that name). The first
  probe takes `origin/foreman/issue-N` as authoritative; if a human
  has pushed weird content there, the Worker will branch from that
  and the Reviewer will catch it. Out of scope for an auto-fallback
  policy.
- The companion "impl-branch naming inconsistency" the issue body
  mentions as a separate ticket. That's a separate filing; this
  spec does not touch `branches.impl_branch` or rename anything.
