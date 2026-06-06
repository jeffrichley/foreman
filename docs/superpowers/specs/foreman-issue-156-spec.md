# Spec: plumb `--pr-url` + target-aware discovery into the Fixer (issue #156)

## Goal

Make `run_fixer` honor its `pr_url` and `target` arguments instead of
silently dropping `pr_url` and hardcoding spec-PR discovery + spec-branch
worktree attach. Today an autonomous `--target impl_pr --pr-url <impl-PR>`
dispatch crashes with "No open PR found for branch foreman/issue-N" because
the role discards `pr_url`, calls `_find_spec_pr` against the spec branch
(already merged for the impl-fix flow), and would mis-attach the spec
worktree even if discovery succeeded. See [#156](https://github.com/jeffrichley/foreman/issues/156).

## Acceptance criteria

- `run_fixer` accepts a new keyword argument `pr_url: str | None = None`
  (added directly after the existing `target` kwarg in
  `packages/foreman/src/foreman/roles/fixer.py`).
- When `pr_url` is non-`None`, `run_fixer` resolves the PR via
  `parse_pr_url(pr_url)` + `repo.get_pull(pr_number)` and does **not** call
  any branch-discovery helper. The repo slug parsed out of `pr_url` must
  match the project's configured repo; a mismatch raises `ValueError` with
  the same shape used by the existing `issue_url` mismatch check.
- When `pr_url` is `None`, `run_fixer` falls back to branch discovery that
  is target-aware:
  - `target == "spec_pr"` discovers by `spec_branch(issue_number)` →
    `foreman/issue-<N>`.
  - `target == "impl_pr"` discovers by `impl_branch(issue_number)` →
    `foreman/impl-<N>`.
- The "No open PR found" `RuntimeError` raised from the fallback path
  names the target in its message (e.g.
  `"No open impl PR found for branch 'foreman/impl-137'"` vs
  `"No open spec PR found for branch 'foreman/issue-137'"`). The message
  no longer hardcodes the word "spec".
- Worktree attach is target-aware: `target == "impl_pr"` calls
  `WorktreeManager.attach_impl(...)`; `target == "spec_pr"` calls
  `WorktreeManager.attach(...)` (today's behavior, unchanged for the spec
  target). This matches the symmetry the Reviewer already implements at
  `packages/foreman/src/foreman/roles/reviewer.py:404-417`.
- `cli.py` no longer drops `pr_url` (`_ = pr_url` at
  `packages/foreman/src/foreman/cli.py:212`). The `fix` command forwards
  `pr_url=pr_url` to `run_fixer`.
- The bare `_find_spec_pr(repo, owner=..., branch=...)` helper is replaced
  with `_find_pr_by_branch(repo, owner=..., branch=..., target=...)`. The
  `target` parameter is used only to build the error string ("spec PR" vs
  "impl PR"); the lookup logic is unchanged (open-state filter +
  `owner:branch` head qualifier).
- `DaemonRunners.run_fixer` in
  `packages/foreman/src/foreman/daemon_runners.py:163-178` continues to
  pass `pr_url=None` (the in-process dispatcher does not yet thread a PR
  number through `Ticket`, and adding that field is explicitly out of
  scope here). The role's discovery fallback handles it correctly because
  the fallback is now target-aware.
- Three new tests in `packages/foreman/tests/test_roles_fixer.py`:
  1. `test_run_fixer_uses_pr_url_when_provided_for_impl_target` —
     dispatches with `target="impl_pr"` and `pr_url=<impl PR URL>`.
     Asserts `repo.get_pull(pr_number)` is called with the parsed number
     and `repo.get_pulls(...)` is **never** called. Simulates the original
     crash by having the fake repo raise from `get_pulls` (so any
     fallback call would fail loudly), and by attaching the worktree on
     a clone where the spec branch has been deleted (mirroring the
     real-world "spec PR merged hours ago" trace).
  2. `test_run_fixer_branch_discovery_is_target_aware_for_impl_target` —
     dispatches with `target="impl_pr"` and no `pr_url`. Asserts the
     `head` qualifier on `repo.get_pulls(...)` is
     `"{owner}:foreman/impl-<N>"`, not `"{owner}:foreman/issue-<N>"`.
  3. `test_run_fixer_impl_discovery_error_message_is_impl_specific` —
     dispatches with `target="impl_pr"` and no `pr_url`, fake repo
     returns `[]` from `get_pulls`. Asserts the `RuntimeError` message
     contains "impl PR" (and does not contain "spec PR").
- Existing tests stay green. The `target="spec_pr"` discovery path
  ([`test_run_fixer_raises_when_no_open_spec_pr`](packages/foreman/tests/test_roles_fixer.py:1043))
  remains valid; its `RuntimeError`-match string updates from
  `"No open PR"` (current) to `"No open spec PR"` to track the new
  message shape.
- `just check` passes (lint + typecheck + tests).

## Approach

The bug has two coupled facets the Worker must address together. Fixing
only one would leave the impl-fix path crashing at the next step.

### 1. PR resolution

`run_fixer` currently takes only `issue_url` and `target`; it derives the
PR via `_find_spec_pr(repo, owner, spec_branch(issue_number))`
([`fixer.py:488-489`](packages/foreman/src/foreman/roles/fixer.py)).
That helper hardcodes the spec-branch convention and the "spec PR" error
string. For `target="impl_pr"`, the right branch is
`impl_branch(issue_number)` and the right error string mentions the impl
PR. The CLI already accepts `--pr-url` and the v3 reconciler already
passes it
([`v3_host.py:386-394`](packages/foreman/src/foreman/reconciler/v3_host.py)),
but `cli.py` drops it at the boundary with a deliberate `_ = pr_url`
placeholder ([`cli.py:207-212`](packages/foreman/src/foreman/cli.py)) and
the role function never accepted it.

The fix follows the Reviewer's existing shape: the Reviewer takes
`pr_url`, calls `parse_pr_url`, and resolves the PR via
`repo.get_pull(pr_number)`
([`reviewer.py:353,368`](packages/foreman/src/foreman/roles/reviewer.py)).
Mirror that in the Fixer: when `pr_url` is provided, resolve directly;
when omitted, fall back to a target-aware branch discovery (rename
`_find_spec_pr` → `_find_pr_by_branch` with a `target` kwarg used only
for the error message).

### 2. Worktree attach

Even after PR resolution is fixed, the Fixer would still call
`wt_mgr.attach(...)` ([`fixer.py:500-506`](packages/foreman/src/foreman/roles/fixer.py))
for both targets. `attach()` checks out the spec branch into the
`issue-<N>/` worktree; the impl-fix flow needs `attach_impl()` which
checks out the impl branch into the `impl-<N>/` worktree (introduced for
the Reviewer-on-impl at
[`worktree.py:486-541`](packages/foreman/src/foreman/worktree.py) and
already exercised at
[`reviewer.py:404-410`](packages/foreman/src/foreman/roles/reviewer.py)).
Without this, a successfully-resolved impl PR would still produce edits +
commits on the wrong branch, missing the impl PR entirely.

Branch the attach the same way the Reviewer does, on the same `target`
kwarg the role already accepts:

```python
if target == "impl_pr":
    wt_path = wt_mgr.attach_impl(...)
else:
    wt_path = wt_mgr.attach(...)
```

`_read_spec_doc` continues to read the spec doc at
`docs/superpowers/specs/foreman-issue-<N>-spec.md` from the attached
worktree — the impl branch is stacked on the spec branch in the v3
model so the file is reachable from either worktree. No change needed
there.

### 3. CLI plumb

Remove the `_ = pr_url` placeholder in `cli.py:212` and forward
`pr_url=pr_url` into the `run_fixer(...)` call at `cli.py:213-222`. The
`--pr-url` option's `help` text already documents that the role uses it
when present and discovers from the branch otherwise; that documentation
becomes accurate after this change.

### 4. Test coverage

The bug surfaced precisely because no test exercised `run_fixer` with
`target="impl_pr"` end-to-end — the existing `test_roles_fixer.py`
covers the per-target prompt selection and the per-target entry-label
precondition (added by foreman#79), but every full `run_fixer` integration
test uses `target="spec_pr"` (the default kwarg). Add three impl-target
integration tests covering the three behaviors above. They reuse the
existing fake-repo/fake-issue scaffolding; the only seed addition is
mirroring `_seed_clone_with_spec_branch` with a sibling
`_seed_clone_with_impl_branch` helper that lays down a
`foreman/impl-<N>` branch on top of seed + spec commits.

## Sub-requests (topologically sorted)

1. In `packages/foreman/src/foreman/roles/fixer.py`, rename
   `_find_spec_pr` to `_find_pr_by_branch` and add a `target: Literal["spec_pr", "impl_pr"]`
   parameter used to build the "spec PR" / "impl PR" wording in the
   `RuntimeError`. Logic otherwise unchanged.
2. In the same file, import `parse_pr_url` from
   `foreman.roles.reviewer` and `impl_branch` from `foreman.branches`
   (already imports `spec_branch`).
3. Add `pr_url: str | None = None` to `run_fixer`'s signature, after
   `target`. Update the docstring's `Args:` block to describe it.
4. In `run_fixer`, replace the unconditional
   `branch = spec_branch(issue_number); pr = _find_spec_pr(...)` block
   with: if `pr_url` is provided, parse + verify repo slug match + call
   `repo.get_pull(pr_number)`; else compute `branch` from
   `spec_branch(issue_number)` (spec_pr) or `impl_branch(issue_number)`
   (impl_pr) and call `_find_pr_by_branch(repo, owner=owner, branch=branch, target=target)`.
5. In `run_fixer`, branch the `wt_mgr.attach(...)` call on `target`:
   `attach_impl(...)` for `impl_pr`, `attach(...)` for `spec_pr`. Path
   arguments are identical between the two methods.
6. In `packages/foreman/src/foreman/cli.py`, delete the `_ = pr_url`
   placeholder line and forward `pr_url=pr_url` into the `run_fixer(...)`
   call inside `fix(...)`.
7. In `packages/foreman/tests/test_roles_fixer.py`, update the existing
   `test_run_fixer_raises_when_no_open_spec_pr` test's `pytest.raises`
   match string from `"No open PR"` to `"No open spec PR"` to track the
   new wording.
8. In the same test file, add a `_seed_clone_with_impl_branch` helper
   mirroring `_seed_clone_with_spec_branch` but creating a
   `foreman/impl-<N>` branch after the spec commit. Reuse the existing
   `_make_fake_repo` helper, parameterized to accept the impl head ref.
9. Add the three new tests listed in the acceptance criteria.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/roles/fixer.py` | Rename `_find_spec_pr` → `_find_pr_by_branch`(target); add `pr_url` kwarg to `run_fixer`; branch PR resolution on `pr_url`-present; branch worktree attach on `target`. |
| `packages/foreman/src/foreman/cli.py` | Forward `pr_url` from the `fix` command into `run_fixer(...)` (delete the `_ = pr_url` placeholder). |
| `packages/foreman/tests/test_roles_fixer.py` | Update the existing spec-discovery error-message test; add `_seed_clone_with_impl_branch` helper; add three impl-target tests covering pr_url-passthrough, target-aware branch discovery, and impl-specific error message. |

`packages/foreman/src/foreman/daemon_runners.py` is **not** modified —
the in-process dispatcher's `Ticket` does not yet carry a PR number, so
`DaemonRunners.run_fixer` continues calling `run_fixer(...)` without
`pr_url` and relies on the now-target-aware fallback. Threading a PR
number through `Ticket` is a larger refactor (it touches the v3
reconciler's snapshot, the SQLite audit row shape, and the
`RealRoleDispatcher` protocol) and is not justified by this issue.

## Alternatives considered

- **Pre-resolve the PR upstream and pass it as a structured object
  through `Ticket` / `ActionContext`.** Rejected: the dispatcher does
  not currently model "open PR" as a first-class field, and the
  refactor would touch the v3 reconciler snapshot, the SQLite audit
  row, and the `_RunnersProtocol` shape. The v3_host already builds the
  URL string from `pr_number` and passes it via subprocess argv —
  mirroring the role function on URL + branch keeps surface area small
  and matches the Reviewer's existing shape.
- **Only fix branch-discovery target-awareness; keep dropping
  `pr_url`.** Rejected: the dispatcher already passes `--pr-url` per
  v3_host.py:386-394 because of the audit-log requirement ("the dispatch
  is self-describing in logs"). Continuing to discard it would re-bury
  the same defect the next time the Reviewer's snapshot disagrees with
  the snapshot the Fixer would discover (e.g., PR list pagination edge
  cases, head-qualifier collisions on forks).
- **Split `run_fixer` into `run_fixer_spec` + `run_fixer_impl`.**
  Rejected: the two flows share ~90% of their body (issue parsing,
  attempt counters, findings extraction, label transitions, JSONL
  stats) and already share a single function in production. The two
  decision points that genuinely differ (entry label, prompt
  composition) are already table-driven via
  `_FIXER_ENTRY_LABEL_BY_TARGET` / `_FIXER_SUPERPOWERS_BY_TARGET`.
  Adding two more table-driven points (discovery branch + worktree
  attach method) is consistent with that pattern.

## Out of scope

- Adding a PR-number field to `Ticket` or `ActionContext`. The
  in-process dispatcher path can continue using `pr_url=None` and the
  now-target-aware fallback.
- Refactoring `_find_pr_by_branch` into a generalized PR-search
  abstraction (e.g., for use by other roles). The function stays a
  module-private helper inside `fixer.py`.
- Auditing the Fixer's parsing of the
  `foreman:findings:begin`/`end` block. The issue mentions this as an
  "adjacent finding" but explicitly notes the JSON shape is well-formed
  and the parser at `_extract_findings_from_review_comment`
  ([`fixer.py:185-227`](packages/foreman/src/foreman/roles/fixer.py))
  already handles it correctly. No change required.
- Changes to the Reviewer, Planner, Worker, or any reconciler rule.
  This is a defect-fix in the Fixer's argument plumbing only.
