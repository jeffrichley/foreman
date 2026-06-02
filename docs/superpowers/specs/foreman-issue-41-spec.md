# Spec: accept impl-branch in reviewer role (issue #41)

## Goal

Widen the Reviewer role so it accepts impl PRs whose head branch is
`foreman/impl-<N>`, not just spec PRs on `foreman/issue-<N>`. Today the
daemon's `RUN_REVIEWER_IMPL` dispatch (`dispatcher.py:29`,
`role_dispatch.py:59-62`) reaches `DaemonRunners.run_reviewer(target="impl_pr")`
(`daemon_runners.py:134-158`) which resolves the impl PR's URL correctly,
but the role function `foreman.roles.reviewer.run_reviewer` then calls
`_issue_number_from_branch` (`reviewer.py:110-124`) which rejects any
branch not matching `^foreman/issue-(?P<number>\d+)$` — so the autonomous
pipeline halts at `foreman:impl-review` and PRs #37 and #40 had to be
human-reviewed during the foreman#30 / foreman#14 dogfood runs.

Tracks issue [#41](https://github.com/jeffrichley/foreman/issues/41).

## Acceptance criteria

- `packages/foreman/src/foreman/roles/reviewer.py` exposes a private
  function `_parse_review_branch(branch: str) -> tuple[int, Literal["spec_pr", "impl_pr"]]`
  that returns the issue number AND the target kind (`"spec_pr"` for
  `foreman/issue-<N>`, `"impl_pr"` for `foreman/impl-<N>`). The legacy
  `_issue_number_from_branch` function is removed; its single internal
  caller (currently at `reviewer.py:292`) is updated.
- `_parse_review_branch` raises `ValueError` for any branch not matching
  either of the two shapes. The error message names BOTH expected shapes
  (`foreman/issue-<N>` and `foreman/impl-<N>`) so an operator running
  `foreman review` against the wrong PR sees what was expected.
- Module-level regex constants in `reviewer.py`: keep `_BRANCH_ISSUE_RE`
  (rename the comment if needed) and add `_BRANCH_IMPL_RE = re.compile(r"^foreman/impl-(?P<number>\d+)$")`.
- Module-level label constants in `reviewer.py`: rename
  `_LABEL_IN_REVIEW` to `_LABEL_SPEC_REVIEW` (for symmetry with the new
  impl constants) and add three new constants —
  `_LABEL_IMPL_REVIEW = "foreman:impl-review"`,
  `_LABEL_READY_FOR_MERGE = "foreman:ready-for-merge"`,
  `_LABEL_IMPL_FIX = "foreman:impl-fix"`. The values match what
  `init.py:91-93` already creates on the repo.
- `run_reviewer` in `reviewer.py:238-364` derives `(issue_number, target)`
  from the PR's head branch immediately after fetching the PR. It then
  picks the in-review / clean-outcome / fix-outcome label triple based
  on `target` and uses that triple for both the pre-flight label check
  and the post-review label transition. No new arguments to the
  function signature — `target` is internal-only.
- The pre-flight label check rejects an impl PR whose source issue is
  missing `foreman:impl-review` (parallel to the existing
  `foreman:spec-review` rejection for spec PRs). The `RuntimeError`
  message names the target-appropriate label.
- The post-review label transition for impl PRs is:
  `foreman:impl-review` → `foreman:ready-for-merge` on `outcome == "clean"`,
  `foreman:impl-review` → `foreman:impl-fix` on `outcome == "needs_fix"`.
  Spec-PR transitions are unchanged.
- `packages/foreman/src/foreman/worktree.py` exposes a new method
  `WorktreeManager.attach_impl(clone_path: Path, repo_slug: str, ticket_id: int) -> Path`
  that mirrors `attach()` (`worktree.py:180-221`) but targets
  `foreman/impl-<N>` and writes the worktree to
  `<worktrees_root>/<repo_slug>/impl-<N>/`. Idempotent on existing path.
  Falls back to `git fetch origin foreman/impl-<N>` when the local
  branch is absent, same defense-in-depth shape `attach()` has.
- `run_reviewer` calls `wt_mgr.attach_impl(...)` instead of
  `wt_mgr.attach(...)` when `target == "impl_pr"`. The diff computation
  (`_get_pr_diff` at `reviewer.py:190-215`) and spec-doc read
  (`_read_spec_doc` at `reviewer.py:218-235`) work unchanged because
  they operate against the resolved worktree path and the base ref from
  the PR object — the spec doc is still on disk in the impl worktree
  (the Worker branched from spec).
- `packages/foreman/src/foreman/cli.py:93-107` (the `review` command)
  updates its docstring to read "Run the Reviewer on a spec PR opened
  by the Planner OR an impl PR opened by the Worker." No code change
  to the command body — it already forwards the URL untouched.
- The `reviewer.py` module docstring at lines 1-27 is updated so the
  "branch derivation" paragraph names both shapes. The
  `Pre-flight guard` paragraph clarifies that the required issue label
  depends on which branch shape the PR carries.
- `packages/foreman/tests/test_roles_reviewer.py`:
  - The two existing parse-tests at lines 54-60 are replaced with three
    parametrized cases: `_parse_review_branch("foreman/issue-42")`
    returns `(42, "spec_pr")`,
    `_parse_review_branch("foreman/impl-42")` returns
    `(42, "impl_pr")`, and `_parse_review_branch("feature/x")` raises
    `ValueError` with a message containing both expected branch shapes.
  - Three new integration tests mirror the existing
    `test_run_reviewer_clean_outcome_advances_to_spec_ready`,
    `test_run_reviewer_needs_fix_outcome_advances_to_spec_fix`, and
    `test_run_reviewer_missing_spec_review_label_raises` but exercise
    the impl-PR path: PR head ref `foreman/impl-42`, issue label
    `foreman:impl-review`, clean outcome advances to
    `foreman:ready-for-merge`, needs_fix outcome advances to
    `foreman:impl-fix`, and missing `foreman:impl-review` raises.
  - The `_seed_clone_with_spec_branch` helper at lines 162-209 grows a
    sibling `_seed_clone_with_impl_branch(clone, issue_number)` that
    seeds an additional `foreman/impl-<N>` branch off the spec branch
    so `attach_impl()` has a local ref to attach to.
  - The import block at lines 22-29 is updated to import
    `_parse_review_branch` instead of `_issue_number_from_branch`.
- `packages/foreman/tests/test_worktree.py` gains a new
  `test_attach_impl_attaches_to_existing_impl_branch` test that
  mirrors the pattern in
  `test_create_impl_creates_dir_with_stacked_branch`
  (`test_worktree.py:635-669`) — seed an impl branch on the clone,
  call `attach_impl()`, verify the worktree path is
  `<root>/<repo>/impl-<N>/` and the checked-out branch is
  `foreman/impl-<N>`.
- `just check` exits zero.

## Approach

The root cause is that the Reviewer was written assuming spec PRs are
the only thing it ever reviews. The Foreman v1 architecture (per
`docs/superpowers/specs/foreman-v1-architectural-spec.md` and the
dispatch table in `dispatcher.py:98-105`) explicitly has the same role
review BOTH the spec PR and the impl PR — they share the
"read a PR, decide clean vs needs_fix, advance a label" shape — so
unifying the two paths inside one `run_reviewer` is the right design.
The dispatcher and `DaemonRunners` already encode that unification at
the layer above (`role_dispatch.py:55-62` routes both `RUN_REVIEWER_SPEC`
and `RUN_REVIEWER_IMPL` to `runners.run_reviewer` with a `target` kwarg).
The role function is the last layer where the spec-only assumption
still lives.

The fix is target-aware label selection driven by branch-shape
detection. We do NOT add a `target` parameter to the role function's
signature — the daemon's `DaemonRunners.run_reviewer` already uses
`target` to pick the branch name when looking up the PR
(`daemon_runners.py:138-142`), and by the time the role sees the PR
URL, the PR's head branch on GitHub IS the authoritative source. Adding
a redundant `target` kwarg would just duplicate the truth and risk
disagreement between "what the daemon claimed" and "what the PR
actually is." Auto-detection from branch shape also keeps the
`foreman review` CLI command working for both spec and impl PR URLs
without exposing a new flag to operators.

The pre-flight label check stays as defense-in-depth: the role refuses
to advance an impl PR unless the source issue carries
`foreman:impl-review` (parallel to the existing `foreman:spec-review`
guard). This catches the case where a human runs `foreman review`
against a stale PR URL whose issue has already been advanced past the
review stage — the existing pattern that prevents accidental
re-advancement of completed work.

The worktree side needs a new `attach_impl()` method because the
existing `attach()` (`worktree.py:180-221`) hardcodes the spec branch
both in the worktree path (`issue-<N>/`) and in the `git worktree add`
target (`foreman/issue-<N>`). For impl PR review, the Reviewer needs
to attach to `foreman/impl-<N>` and live at `impl-<N>/`. We add a
sibling method rather than parameterizing `attach()` because the
existing pairing already follows the `create` / `create_impl` shape
(`worktree.py:81-178`) — sticking to the established API symmetry is
clearer than adding a `branch_kind: Literal["spec", "impl"]` parameter
that would change `attach()`'s signature for one caller. The impl
worktree IS already created (by the Worker via `create_impl` at
`worktree.py:124-178`) and pushed; `attach_impl()` is the read-side
counterpart for downstream roles.

The label constant rename (`_LABEL_IN_REVIEW` → `_LABEL_SPEC_REVIEW`)
is included because the new code introduces `_LABEL_IMPL_REVIEW` and
the asymmetric pair (`_LABEL_IN_REVIEW` + `_LABEL_IMPL_REVIEW`) would
read confusingly. The rename is local to `reviewer.py` — these are
private module constants with no external imports (confirmed via
Grep: only the production file and its own test file reference the
old name, and the test file references it only through public
behavior, not the constant directly).

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/roles/reviewer.py`:
   - Add `from typing import Literal` to the imports (it's not
     currently imported).
   - Add the new regex constant under the existing
     `_BRANCH_ISSUE_RE` (`reviewer.py:50`):
     `_BRANCH_IMPL_RE = re.compile(r"^foreman/impl-(?P<number>\d+)$")`.
   - Rename `_LABEL_IN_REVIEW` to `_LABEL_SPEC_REVIEW` at
     `reviewer.py:58`. Add three new label constants below the spec
     ones: `_LABEL_IMPL_REVIEW = "foreman:impl-review"`,
     `_LABEL_READY_FOR_MERGE = "foreman:ready-for-merge"`,
     `_LABEL_IMPL_FIX = "foreman:impl-fix"`.
2. In the same file, replace `_issue_number_from_branch`
   (`reviewer.py:110-124`) with `_parse_review_branch`:

   ```python
   def _parse_review_branch(branch: str) -> tuple[int, Literal["spec_pr", "impl_pr"]]:
       """Derive ``(issue_number, target)`` from a Reviewer-eligible head branch.

       Two valid shapes:
       - ``foreman/issue-<N>`` → spec PR review (``target="spec_pr"``)
       - ``foreman/impl-<N>``  → impl PR review (``target="impl_pr"``)

       Raises ``ValueError`` on any other shape — the Reviewer only acts
       on PRs produced by the Planner or the Worker, both of which use
       the conventions above.
       """
       m = _BRANCH_ISSUE_RE.match(branch)
       if m is not None:
           return int(m["number"]), "spec_pr"
       m = _BRANCH_IMPL_RE.match(branch)
       if m is not None:
           return int(m["number"]), "impl_pr"
       raise ValueError(
           f"PR head branch {branch!r} is not a Foreman review branch "
           "(expected 'foreman/issue-<N>' for spec PRs or "
           "'foreman/impl-<N>' for impl PRs)."
       )
   ```

3. In the same file, update the module docstring lines 14-21 so the
   branch-derivation paragraph reads:

   ```
   The label transition is on the originating ISSUE, not the PR — same
   pattern the Planner uses. The Reviewer derives the issue number and
   the review target (spec PR vs impl PR) from the PR's head branch:
   - foreman/issue-<N> → spec PR (label foreman:spec-review)
   - foreman/impl-<N>  → impl PR (label foreman:impl-review)

   Pre-flight guard: if the source issue does not carry the
   target-appropriate review label, the orchestrator raises before
   doing any work — we will not silently advance a PR whose source
   issue was not queued for review.
   ```

4. In the same file, update the `run_reviewer` body
   (`reviewer.py:238-364`) so that after fetching the PR at line 287:
   - Replace line 292's `issue_number = _issue_number_from_branch(head_branch)`
     with `issue_number, target = _parse_review_branch(head_branch)`.
   - Immediately after, derive the label triple:

     ```python
     if target == "impl_pr":
         in_review_label = _LABEL_IMPL_REVIEW
         clean_label = _LABEL_READY_FOR_MERGE
         fix_label = _LABEL_IMPL_FIX
     else:
         in_review_label = _LABEL_SPEC_REVIEW
         clean_label = _LABEL_SPEC_READY
         fix_label = _LABEL_SPEC_FIX
     ```

   - Replace the pre-flight check at lines 296-302 so it uses
     `in_review_label` in the membership test AND in the error message
     (so the message names the correct label for the target kind).
   - Replace the worktree attach call at lines 307-312:

     ```python
     wt_mgr = WorktreeManager(worktrees_root=worktrees_root)
     if target == "impl_pr":
         wt_path = wt_mgr.attach_impl(
             clone_path=Path(project.local_clone_path),
             repo_slug=repo_name,
             ticket_id=issue_number,
         )
     else:
         wt_path = wt_mgr.attach(
             clone_path=Path(project.local_clone_path),
             repo_slug=repo_name,
             ticket_id=issue_number,
         )
     ```

   - Replace the label-transition block at lines 354-362:

     ```python
     if llm_output.outcome == "clean":
         add_label = clean_label
     else:
         add_label = fix_label
     issue.remove_from_labels(in_review_label)
     issue.add_to_labels(add_label)
     ```

5. In `packages/foreman/src/foreman/worktree.py`, add a new method
   `attach_impl` after `attach` (insert at `worktree.py:222`, right
   before the `cleanup` method). Body mirrors `attach()` literally,
   with two changes: the worktree path uses `f"impl-{ticket_id}"` and
   the branch literal uses `f"foreman/impl-{ticket_id}"`. Docstring
   describes it as the read-side counterpart of `create_impl` —
   attaches to the impl branch the Worker pushed.

6. In `packages/foreman/src/foreman/cli.py:93-107`, update the
   `review` command's docstring from `"""Run the Reviewer on a spec
   PR opened by the Planner."""` to:

   ```python
   """Run the Reviewer on a spec PR opened by the Planner OR an impl
   PR opened by the Worker.

   The Reviewer derives spec-vs-impl from the PR's head branch shape
   (foreman/issue-<N> vs foreman/impl-<N>) — no flag required.
   """
   ```

7. In `packages/foreman/tests/test_roles_reviewer.py`:
   - Update the import block at lines 22-29: replace
     `_issue_number_from_branch` with `_parse_review_branch`.
   - Replace the two parse-tests at lines 54-60 with three:

     ```python
     def test_parse_review_branch_parses_spec_branch() -> None:
         assert _parse_review_branch("foreman/issue-42") == (42, "spec_pr")

     def test_parse_review_branch_parses_impl_branch() -> None:
         assert _parse_review_branch("foreman/impl-42") == (42, "impl_pr")

     def test_parse_review_branch_rejects_unrelated_branch() -> None:
         with pytest.raises(ValueError, match="foreman/issue-<N>.*foreman/impl-<N>"):
             _parse_review_branch("feature/some-thing")
     ```

   - Add `_seed_clone_with_impl_branch(clone, issue_number) -> str`
     helper below `_seed_clone_with_spec_branch` at
     `test_roles_reviewer.py:162-209`. It runs the spec-seeding
     helper first (the impl branch is stacked on spec), then
     `git checkout -b foreman/impl-<N>` from the spec branch, adds a
     dummy code file (e.g. `src/foo.py`), commits, returns the new
     HEAD sha. Returns to `main` at the end.
   - Add three new integration tests mirroring the existing
     spec-path ones at lines 286-352, 446-483, 539-571 but with PR
     head ref `foreman/impl-42`, issue labels `foreman:impl-review`,
     and asserted label transitions
     `["foreman:impl-review"] → ["foreman:ready-for-merge"]` on
     clean and `["foreman:impl-review"] → ["foreman:impl-fix"]` on
     needs_fix, and an
     `test_run_reviewer_missing_impl_review_label_raises` that uses
     a non-foreman label on the issue and confirms `RuntimeError`
     matching `foreman:impl-review`.

8. In `packages/foreman/tests/test_worktree.py`, add
   `test_attach_impl_attaches_to_existing_impl_branch` after
   `test_create_impl_filters_env_on_git_subprocess_calls` at
   `test_worktree.py:697`. Pattern: seed a clone with a `foreman/impl-42`
   branch (use the same `subprocess.run` git scaffolding the existing
   `create_impl` tests use at lines 635-668), call
   `WorktreeManager.attach_impl(clone, repo_slug, 42)`, assert the
   returned path is `<root>/<repo>/impl-42/` and `git branch --show-current`
   in the worktree returns `foreman/impl-42`.

9. Run `just check` and confirm exit zero.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/roles/reviewer.py` | Add `_BRANCH_IMPL_RE`; rename `_LABEL_IN_REVIEW` → `_LABEL_SPEC_REVIEW`; add `_LABEL_IMPL_REVIEW` / `_LABEL_READY_FOR_MERGE` / `_LABEL_IMPL_FIX`; replace `_issue_number_from_branch` with `_parse_review_branch` returning `(int, target)`; rewrite `run_reviewer` body to derive a label triple from `target` and branch on `target` for the worktree attach + pre-flight check + post-review label transition; update module docstring to name both shapes. |
| `packages/foreman/src/foreman/worktree.py` | Add `WorktreeManager.attach_impl(clone_path, repo_slug, ticket_id)` mirroring `attach()` but targeting `foreman/impl-<N>` at path `<root>/<repo>/impl-<N>/`. |
| `packages/foreman/src/foreman/cli.py` | Update `review` command docstring (lines 93-94) to mention both spec and impl PRs. |
| `packages/foreman/tests/test_roles_reviewer.py` | Replace two parse-tests with three (spec, impl, reject); add `_seed_clone_with_impl_branch` helper; add three impl-path integration tests (clean → ready-for-merge, needs_fix → impl-fix, missing impl-review label raises). |
| `packages/foreman/tests/test_worktree.py` | Add one `attach_impl` integration test. |

No changes to `dispatcher.py`, `role_dispatch.py`, `daemon_runners.py`,
`init.py`, `roles/worker.py`, or `roles/fixer.py`. The dispatcher
already routes both review actions correctly; the labels are already
created on the repo by `init.py`; the Worker already pushes
`foreman/impl-<N>` (confirmed at `worker.py:510, 651`).

## Alternatives considered

- **Add a `target: Literal["spec_pr", "impl_pr"]` parameter to
  `run_reviewer` and propagate it from `DaemonRunners`.** Rejected:
  duplicates information the PR's head branch already carries
  authoritatively. The daemon's `target` parameter is used at the
  `DaemonRunners` layer to compute which branch to LOOK UP the PR for
  (`daemon_runners.py:138-142`); once we have the PR URL, the PR's
  head branch on GitHub IS the source of truth. Adding a redundant
  kwarg would also force the `foreman review` CLI command to grow a
  `--target` flag (or auto-detect anyway), which is the opposite of
  what the issue body recommends.
- **Parameterize `WorktreeManager.attach()` with `branch_kind:
  Literal["spec", "impl"] = "spec"` instead of adding `attach_impl()`.**
  Rejected: breaks the established API symmetry where
  `create` / `create_impl` are sibling methods (`worktree.py:81-178`).
  A new sibling method is more discoverable and reads better at the
  call site (`wt_mgr.attach_impl(...)` vs
  `wt_mgr.attach(..., branch_kind="impl")`).
- **Have the Reviewer share the Worker's `impl-<N>/` worktree
  on-disk instead of creating its own.** Rejected: the Worker's
  worktree may be cleaned up between Worker run and Reviewer run, and
  even when present it may be on a stale commit if the Worker pushed
  after a rebase. The Reviewer needs an independent attach that
  fetches the latest impl branch from origin — same pattern the
  spec-side `attach()` uses.
- **Pull the foreman.branches centralization (from issue #49's spec)
  forward into this ticket, importing `impl_branch` / `spec_branch`
  helpers.** Rejected: issue #49 may or may not have landed by the
  time this spec executes, and coupling #41 to #49's status would
  block the autonomous-loop fix on an unrelated refactor. The two
  new regex constants in `reviewer.py` are isolated to one file; if
  #49 lands later, a tiny follow-up can replace them with imports
  from `foreman.branches`. This is the same pattern #49's spec
  itself follows for keeping test files independent of the helpers.
- **Also fix the Worker bot-identity bug (Worker commits attributed
  to `foreman-planner[bot]`) in this spec.** Rejected: explicitly
  flagged as a "Companion" issue in the bug body ("Pairs with the
  Worker bot-identity bug..."). The author drew the line; we honor
  it. Conflating two distinct fixes makes the diff harder to review
  and forces the Worker to land both atomically or neither. See
  "Out of scope" below.
- **Do nothing — instruct operators to manually review impl PRs.**
  Rejected: the issue body labels this severity High and explicitly
  notes "the loop physically cannot finish without it." This is the
  missing final stage of the autonomous pipeline; deferring it
  defeats v1's stated end-to-end goal.

## Open questions

(none — the failure mode is fully reproducible from the issue body's
stacktrace, the dispatcher and daemon-runner layers above the role
function already implement the target distinction, the labels are
already created by `init.py:91-93`, and the Worker is already pushing
`foreman/impl-<N>` exactly as the new regex expects. The fix is
mechanical at the role and worktree layers.)

## Out of scope

- The Worker bot-identity bug (Worker commits showing
  `foreman-planner[bot]` as author). Explicitly called out by the
  issue author as a "Companion" issue. Belongs in a separate ticket
  about the identity-registry's role-switch behavior when attaching
  to an existing worktree the Planner created.
- Centralizing branch-name construction into `foreman.branches`. That
  is issue #49's spec (in `docs/superpowers/specs/foreman-issue-49-spec.md`,
  already drafted and in flight); doing it again here would duplicate
  the refactor.
- Teaching the Fixer to accept impl branches as well. The Fixer is a
  separate role with its own dispatch path (`RUN_FIXER_IMPL`); the
  same class of bug exists in `roles/fixer.py:434` (hardcoded
  `f"foreman/issue-{issue_number}"`), but the issue body is
  specifically about the Reviewer's blocking the final stage. The
  Fixer's impl-path bug should get its own ticket so the diff is
  scoped to one role.
- Refactoring `DaemonRunners.run_reviewer` to stop computing a
  `branch` from `target` (since the role now derives target
  authoritatively from the branch). The daemon-runner layer still
  needs the branch string to call `find_pr_for_branch` BEFORE the
  role sees anything — that lookup is the only way to map a ticket
  to a PR number. The two layers use the branch shape for different
  reasons and the duplication is intrinsic.
- Adding a runtime check that `DaemonRunners`'s asserted `target`
  matches the PR's actual head branch shape. The PR-lookup
  (`find_pr_for_branch`) already enforces this implicitly — if no PR
  exists for the expected branch, it raises before the role runs.
  Adding a second assertion in the role would be redundant.
- Updating `roles/reviewer.py`'s vendored `requesting-code-review`
  superpowers prompt to acknowledge impl PRs. The prompt is
  PR-shape-agnostic ("review this PR"); no edits needed.
