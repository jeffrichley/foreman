# Spec: plumb `--target` through `run_reviewer` as a sanity-check assertion against `head_ref` (issue #198)

## Goal

Close the gap left by commit `41585c9` (the Stage-2 dispatch-action
split that introduced `foreman review --target {spec_pr,impl_pr}` to
fix HIGH #7's target-ambiguity bug). Today the CLI accepts `--target`
but discards it with `_ = target` before invoking `run_reviewer`,
because the role's signature was left untouched as "out of scope" at
the time. This spec lands the deferred role-side change: thread
`--target` into `run_reviewer` as an *optional* kwarg, and assert
inside the role that the caller-supplied target matches the target
derived from the PR's head branch — raising `RuntimeError` on
mismatch. Head-ref stays the canonical inference path; `--target`
becomes a defensive cross-check at the role/action boundary. See
issue [#198](https://github.com/jeffrichley/foreman/issues/198).

## Acceptance criteria

- `run_reviewer()` in
  `packages/foreman/src/foreman/roles/reviewer.py:314` accepts a new
  optional kwarg
  `target: Literal["spec_pr", "impl_pr"] | None = None`, added after
  `identity_registry`. The kwarg defaults to `None` so all existing
  call sites continue to work unchanged.
- Inside `run_reviewer`, AFTER the existing
  `issue_number, target = _parse_review_branch(head_branch)` call
  (today at `reviewer.py:373`), the local variable derived from the
  head ref MUST be renamed (e.g., `head_target`) so it does not
  collide with the new kwarg. The caller-supplied kwarg keeps the
  name `target`.
- A mismatch guard runs immediately after `_parse_review_branch`:
  ```python
  if target is not None and target != head_target:
      raise RuntimeError(
          f"target mismatch: --target={target!r} but PR head_ref="
          f"{head_branch!r} implies {head_target!r}"
      )
  ```
  The error message MUST include both the caller-supplied target,
  the PR head ref, and the head-derived target literally, so that
  the failure is self-describing in the daemon's audit log.
- All in-function references to the head-derived target downstream
  of this point (today `target` at lines 375, 376, 404 of
  `reviewer.py`) use the renamed local `head_target` — head-ref
  remains canonical for routing (label lookup, worktree-attach
  branch, prompt composition).
- `cli.py` `review` command (today at
  `packages/foreman/src/foreman/cli.py:110-140`) passes
  `target=target` to `run_reviewer` and DELETES the
  `# ``target`` is accepted for symmetry … out of scope for the
  Stage-2 action split.` comment block (today at
  `cli.py:126-130`) AND the `_ = target` line at `cli.py:131`.
  The click option block (`@click.option("--target", …)` at
  `cli.py:92-102`) and its help text are NOT modified.
- The `review` function docstring at `cli.py:116-122` is updated to
  remove the misleading phrase "currently advisory (the role infers
  target from the PR itself)" — after this change `--target` is no
  longer advisory; it is authoritative-but-cross-checked. Replace
  with a one-clause statement that the role cross-checks the
  caller-supplied target against the PR head ref.
- Three new tests in `packages/foreman/tests/test_roles_reviewer.py`
  (the canonical Reviewer-test location in this repo — note: the
  issue body refers to `tests/roles/test_reviewer.py`, which is not
  the actual layout used by Foreman; the right file is
  `packages/foreman/tests/test_roles_reviewer.py`):
  - `test_run_reviewer_accepts_matching_target_arg`: builds a
    spec-shaped PR (head_ref `foreman/issue-42`, issue labeled
    `foreman:planning`), invokes
    `run_reviewer(..., target="spec_pr")`, asserts normal flow
    proceeds (LLM dispatched, review posted, label transitioned).
    Reuse the existing fakes (`_FakePR`, `_FakeIssue`, etc.) and
    seed helpers (`_seed_clone_with_spec_branch`, `_make_config`,
    etc.) — do NOT introduce new scaffolding.
  - `test_run_reviewer_accepts_none_target`: same setup as the
    existing
    `test_run_reviewer_clean_outcome_advances_to_plan_approved`
    but explicitly passes `target=None` so the new default path is
    exercised by name. Asserts the same flow completes.
  - `test_run_reviewer_raises_on_target_mismatch`: builds a
    spec-shaped PR (head_ref `foreman/issue-42`), invokes
    `run_reviewer(..., target="impl_pr")`, expects
    `pytest.raises(RuntimeError)` and asserts the error message
    contains the literal strings `"impl_pr"`, `"foreman/issue-42"`,
    and `"spec_pr"` so all three variables named in the error are
    surfaced. No PR review is posted (assert
    `pr.reviews_posted == []`). No label transition occurs (assert
    `issue.set_labels_calls == []`).
- All existing Reviewer tests (24 tests in
  `packages/foreman/tests/test_roles_reviewer.py`) still pass
  unchanged. The new kwarg is optional and defaults to `None`, so
  every existing call site goes through the `target is None` branch
  of the guard and produces identical behavior.
- `just check` exits 0 (the full quality gate: ruff lint, mypy
  typecheck, pytest). The branded total in the issue body ("924+
  tests") is approximate — the criterion is "all tests pass plus
  the three new ones," not a specific count.
- `grep '_ = target' packages/foreman/src/foreman/cli.py` returns
  zero results after the change. (Note: `_ = pr_url` in the `fix`
  command at `cli.py:212` is NOT touched — only the Reviewer's
  `_ = target` is removed.)

## Approach

The deferred-plumbing call-out in commit `41585c9` documents itself:
the CLI added `--target` so the v3 dispatch-action layer could split
`DISPATCH_REVIEWER_SPEC` from `DISPATCH_REVIEWER_IMPL` (fixing HIGH
#7), but the Reviewer role infers target from the PR head branch
via `_parse_review_branch` — so forwarding the CLI flag into the
role required a signature change, which the rescue PR explicitly
declined to take on. Issue #198 asks us to close that loop.

The fix is small, local, and conservative:

1. **Head-ref stays canonical.** `_parse_review_branch` already
   returns `(issue_number, target)` from the branch convention
   (`foreman/issue-<N>` → `spec_pr`, `foreman/impl-<N>` →
   `impl_pr`). That is the right ground truth — the branch shape is
   what determines which prompt to load, which entry label to
   demand on the issue, and which worktree-attach helper to use.
   We do not move that inference; we only ADD a cross-check.

2. **Caller-supplied `target` is a defensive contract.** When the
   v3 dispatch action emits `DISPATCH_REVIEWER_SPEC` it passes
   `--target spec_pr`. When it emits `DISPATCH_REVIEWER_IMPL` it
   passes `--target impl_pr`. If a future rule misfires (e.g., a
   predicate filter that lets an impl PR through the spec branch
   of the dispatcher), the head ref and the `--target` will
   disagree — and the Reviewer will crash with an actionable
   error message naming all three values rather than silently
   reviewing the wrong PR shape. This is exactly the class of bug
   HIGH #7 lived in, and the safety net belongs at the
   role/action boundary.

3. **Mirrors `run_fixer`.** The Fixer already takes
   `target: str = "spec_pr"`
   (`packages/foreman/src/foreman/roles/fixer.py:399`) and uses
   the value as authoritative routing input. The Reviewer
   diverges intentionally: head-ref is canonical for the Reviewer
   because the head branch is what's open on the PR being
   reviewed, while the Fixer is invoked on an ISSUE and the
   branch comes from the issue's queue label. The cross-check
   here brings the *signature shape* to parity (both roles accept
   `target` as a kwarg from the dispatch action) without claiming
   the *routing semantics* are identical.

4. **Defaulting to `None`.** Tests, local CLI invocations, and any
   external caller that omits `--target` continue to work. The
   guard's `target is not None and …` form makes the
   backward-compatible path obvious.

5. **Naming.** The kwarg `target` would shadow the head-derived
   local `target` returned from `_parse_review_branch`. Renaming
   the local to `head_target` makes the comparison
   (`target != head_target`) read clearly and isolates the rename
   to one function; downstream uses of the head-derived value
   (label-table lookup, worktree-attach branch selector, prompt
   composition) are mechanical find-and-replace within
   `run_reviewer`.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/roles/reviewer.py`, update the
   `run_reviewer` signature (today at line 314) to add a new
   optional kwarg
   `target: Literal["spec_pr", "impl_pr"] | None = None` immediately
   after `identity_registry`. Update the docstring's `Args:` block
   to describe `target` as "optional caller-supplied target; when
   provided, cross-checked against the head-branch-derived target
   and raises `RuntimeError` on mismatch. None preserves the
   pre-#198 behavior where head ref is the only signal."
2. In the same function, rename the local destructured from
   `_parse_review_branch` from `target` to `head_target`:
   `issue_number, head_target = _parse_review_branch(head_branch)`.
3. Immediately after that line, insert the guard:
   ```python
   if target is not None and target != head_target:
       raise RuntimeError(
           f"target mismatch: --target={target!r} but PR head_ref="
           f"{head_branch!r} implies {head_target!r}"
       )
   ```
   Include a one-line comment immediately above the guard
   referencing issue #198 and the
   "role/action boundary safety net" framing from the Approach
   section.
4. In the same function, replace the three remaining uses of the
   old `target` local (today at lines 375, 376, 404) with
   `head_target` so head-ref-derived routing is unchanged.
5. In the `run_reviewer` docstring's `Raises:` block, add a new
   bullet for the new error: ``RuntimeError`` — "caller-supplied
   `target` disagrees with the PR head branch's derived target
   (defensive cross-check; see issue #198)."
6. In `packages/foreman/src/foreman/cli.py`, in the `review`
   command body (today at lines 110-140):
   - Delete the `# ``target`` is accepted for symmetry …` comment
     block at lines 126-130 in its entirety.
   - Delete the `_ = target` line at line 131.
   - Pass `target=target` as an extra kwarg to the
     `run_reviewer(...)` call at line 133.
7. In the same file, update the `review` command's function
   docstring at lines 116-122 by replacing the sentence "Reviewer
   derives spec-vs-impl from the PR's head branch shape
   (foreman/issue-<N> vs foreman/impl-<N>); ``--target`` is
   accepted but currently advisory (the role infers target from
   the PR itself)." with: "Reviewer derives spec-vs-impl from the
   PR's head branch shape (foreman/issue-<N> vs
   foreman/impl-<N>); when ``--target`` is supplied (e.g., from
   the v3 dispatch action), the role cross-checks it against the
   head-derived target and raises on mismatch."
   The `@click.option("--target", …)` block at lines 92-102 is
   NOT modified.
8. In `packages/foreman/tests/test_roles_reviewer.py`, add new
   test `test_run_reviewer_accepts_matching_target_arg` placed
   adjacent to `test_run_reviewer_clean_outcome_advances_to_plan_approved`
   (lines 418-491). Use the same fixtures (`tmp_path`, monkeypatch,
   `_seed_clone_with_spec_branch`, `_make_config`,
   `_make_fake_repo`, `_FakeReviewerClient`, `_make_registry`,
   `_make_clean_output`). Invoke
   `await run_reviewer(..., target="spec_pr", ...)` and assert
   `result.llm_output.outcome == "clean"` and `len(pr.reviews_posted)
   == 1` to confirm normal flow.
9. In the same file, add new test
   `test_run_reviewer_accepts_none_target`: identical to
   sub-request 8 except pass `target=None` explicitly. Asserts
   the same `result.llm_output.outcome == "clean"` and
   `len(pr.reviews_posted) == 1` to confirm the explicit-None path
   works.
10. In the same file, add new test
    `test_run_reviewer_raises_on_target_mismatch`: builds a
    spec-shaped PR (head_ref `foreman/issue-42`), invokes
    `await run_reviewer(..., target="impl_pr", ...)`, uses
    `pytest.raises(RuntimeError) as exc_info`, and asserts the
    string representation of the exception contains all three
    literal substrings: `"impl_pr"`, `"foreman/issue-42"`,
    `"spec_pr"`. Additionally asserts no review was posted
    (`pr.reviews_posted == []`) and no label set was written
    (`issue.set_labels_calls == []`) — the guard fires before any
    side effects.
11. Run `just check`. Resolve any drift (no new imports expected
    in the source change; the test file already imports
    `pytest.raises` patterns and the helper scaffolding). All
    three new tests must pass; all existing tests must continue to
    pass.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/roles/reviewer.py` | Add `target: Literal["spec_pr", "impl_pr"] | None = None` kwarg to `run_reviewer`; rename head-derived local from `target` to `head_target`; insert mismatch guard that raises `RuntimeError` with self-describing message; update docstring `Args:` and `Raises:` blocks. |
| `packages/foreman/src/foreman/cli.py` | In `review` command: delete the "out of scope for Stage-2" comment block and `_ = target` line, pass `target=target` into `run_reviewer`, refresh the function docstring's "advisory" sentence to describe the new cross-check semantics. The click option help text is unchanged. |
| `packages/foreman/tests/test_roles_reviewer.py` | Add three new tests (`test_run_reviewer_accepts_matching_target_arg`, `test_run_reviewer_accepts_none_target`, `test_run_reviewer_raises_on_target_mismatch`) using the existing fixtures and seed helpers. |

No changes are required to:

- `packages/foreman/src/foreman/reconciler/actions.py` or
  `packages/foreman/src/foreman/reconciler/rules.py` — the
  dispatch action already passes `--target` and is correct; this
  spec only adds a downstream check.
- `packages/foreman/src/foreman/roles/fixer.py` — Fixer already
  takes `target`; this spec brings Reviewer to parity, not the
  other way around.
- `packages/foreman/src/foreman/dispatcher.py` or
  `packages/foreman/src/foreman/daemon_runners.py` — no
  identity, queue, or daemon-level wiring changes.
- The Reviewer's schemas (`packages/foreman/src/foreman/schemas/reviewer.py`)
  — no new output fields; the guard only affects control flow.

## Alternatives considered

- **Make `target` required (no default) and update every call
  site explicitly.** Rejected: forces a coordinated change across
  every test that constructs `run_reviewer` calls (24 existing
  tests in `test_roles_reviewer.py`) plus any future external
  callers, for no benefit — head_ref is already canonical, and
  `None` cleanly expresses "no cross-check requested" without
  losing safety guarantees for the v3 dispatch path that does
  pass it.
- **Have the CLI build the cross-check itself before calling
  `run_reviewer`.** Rejected: the CLI doesn't have the PR
  fetched at that point — `pr.head.ref` is read inside
  `run_reviewer` via the PyGithub client the role constructs.
  Fetching it twice (once in the CLI for the check, once in the
  role for the actual work) is wasted API calls AND splits the
  invariant across two files, exactly the "contracts spread
  across files" pattern foreman#190 was about avoiding.
- **Promote the cross-check to the dispatcher (action handler)
  layer.** Rejected: the dispatcher emits the CLI argv and
  fires the subprocess; it does not see the PR's head branch
  either. The role IS the natural enforcement point because it
  is the first place where both inputs (caller-supplied
  `target` + PR head ref) are visible in the same scope.
- **Leave it as-is and rely on the dispatcher emitting correct
  argv.** Rejected: this is the status quo and the issue body
  explains why it's wrong — the safety net is precisely for
  the case where a future rule predicate misfires. Without the
  guard, a wrong dispatch silently reviews the wrong PR shape
  and the bug is detected far downstream.

## Open questions

- The issue's "Related" section references
  `docs/architecture/v3-reconciler.md` §7 item 1 as a drift entry
  to be removed after this lands. That file does not exist in
  this repo — the v3 architecture material lives in
  `docs/superpowers/plans/` (four plans, the most recent being
  `2026-06-04-foreman-v3-runtime-wiring-implementation.md`) and
  in `docs/superpowers/specs/`. Recommend: the Worker should
  NOT attempt to create or update a non-existent doc; document
  the closure of this drift in the impl PR body instead so
  whoever later authors `v3-reconciler.md` has the audit trail.
  If the architecture doc does land before this issue closes,
  the §7 item-1 cleanup can be a small follow-up.

## Out of scope

- Changing `_parse_review_branch`'s behavior or contract — head
  ref stays canonical. Issue body Out-of-scope.
- Removing `--target` from the CLI — explicitly preserved for
  dispatch-action symmetry with Fixer. Issue body Out-of-scope.
- Adding similar plumbing to the Fixer — Fixer already takes
  `target` as a kwarg; this spec brings Reviewer to parity.
  Issue body Out-of-scope.
- Modifying the `@click.option("--target", …)` block's help
  text. The current help text remains accurate after the
  change. Issue body Out-of-scope.
- Touching rule predicates or action handlers in
  `packages/foreman/src/foreman/reconciler/` — this is a
  role-internal fix. Issue body Out-of-scope.
- Refactoring `_FIXER_ENTRY_LABEL_BY_TARGET` or
  `_REVIEWER_ENTRY_LABEL_BY_TARGET` shape. Both maps are at
  module scope today and work correctly; this spec reuses them
  as-is.
- Adding a similar cross-check to the Fixer's existing `target`
  kwarg. The Fixer's `target` is authoritative routing input
  (derived from the issue's queue label by the dispatch
  action), not redundant-with-head-ref the way Reviewer's is.
  No cross-check is needed there.
- Creating or editing `docs/architecture/v3-reconciler.md`
  (see Open questions for rationale).
