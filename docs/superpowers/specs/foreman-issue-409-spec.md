# Spec: `foreman reset` refuses by default on closed issues, adds `--force-reopen` (issue #409)

## Goal

Fix the silent no-op that occurs when `foreman reset` is run on a CLOSED GitHub
issue and the re-trigger step applies `foreman:plan` — a label the Poller ignores
on closed issues. The fix, per @wrenrichley's correction in the issue comments,
is **refuse-by-default** (loud error) rather than auto-reopen (which would silently
resurrect already-completed tickets). An opt-in `--force-reopen` flag covers the
legitimate "this ticket was erroneously closed and I want to restart it" case.
Tracks [foreman#409](https://github.com/jeffrichley/foreman/issues/409).

## Acceptance criteria

- Calling `foreman reset --project P --issue-number N` (without `--force-reopen`)
  on a **closed** issue exits non-zero and prints a message naming that the issue
  is closed, that it may already be complete, and that `--force-reopen` is required
  to proceed. The error fires regardless of `--dry-run`.
- Calling `foreman reset --project P --issue-number N --force-reopen` on a closed
  issue reopens the issue AND applies `foreman:plan`, so the Poller picks it up on
  the next tick.
- `--no-retrigger` bypasses the closed-issue guard entirely: no reopen, no
  `foreman:plan` label, no error (the operator is explicitly opting out of
  re-triggering, so the issue's open/closed state is irrelevant).
- `--dry-run` on a closed issue (without `--force-reopen`) still exits non-zero
  and shows the warning — the guard fires before the plan is printed.
- `--dry-run` on a closed issue with `--force-reopen` shows `Reopen issue #N` as a
  plan step (before `Apply foreman:plan label`) and exits 0 without executing.
- When the issue is open, behaviour is unchanged across all flag combinations.
- New test covering: (a) closed+no-flag → exit 1 + warning, (b) closed+force-reopen
  → reopened + label applied, (c) open issue → unchanged behaviour, (d) closed +
  no-retrigger → no error.

## Approach

**Pattern naming (Decision 4 — calibrated lens).** No GoF pattern applies to the
core of this change — the guard is straightforward conditional logic layered on top
of an existing CLI command. The relevant Google engineering principle is **"make the
right thing easy, the dangerous thing loud"**: the default (refuse + warn) is the
safe path; the dangerous escape (`--force-reopen`) is opt-in. That principle drives
every decision below.

**Where the check lives.** `_discover()` in
`packages/foreman/src/foreman/v4/cli/mutations.py` already makes all read-only
GitHub API calls (label fetch, branch lookup, PR discovery). Add `git.get_issue_state()`
here and populate two new `ResetPlan` booleans: `issue_is_closed` and `reopen_issue`.
`issue_is_closed` is always set; `reopen_issue` is True only when `issue_is_closed
and retrigger and force_reopen`. This keeps `_discover()` as the single canonical
"build the plan" function without polluting `_plan_steps()` or `_execute()` with
read logic.

**Where the guard fires.** In `cmd_reset()`, immediately after `_plan_steps()` and
before `_render_plan()`. The condition is:

```python
if plan.issue_is_closed and plan.apply_plan_label and not plan.reopen_issue:
    typer.echo(
        f"issue #{issue_number} is closed (it may already be complete — "
        f"check for a merged PR before re-triggering); "
        f"pass --force-reopen to reset and reopen anyway.",
        err=True,
    )
    raise typer.Exit(code=1)
```

`plan.apply_plan_label` is False when `--no-retrigger` is set, so the guard never
fires in that case. The guard fires **before** the dry-run return, so `--dry-run`
also exits 1 with the warning visible on stderr.

**New `GitProvider` methods.** Two narrowly-scoped additions to the Protocol:
- `get_issue_state(*, project, issue_number) -> str` — returns `"open"` or `"closed"`
  (GitHub's two values for `issue.state`).
- `reopen_issue(*, project, issue_number) -> None` — idempotent reopen; no-op if
  already open (mirrors `close_issue`'s already-closed idempotency).

These are added to `GitProvider` (Protocol), `FakeGitProvider`, `PyGithubGitProvider`,
and `RoutingGitProvider`, following the exact same four-file pattern used by every
other Protocol method addition in this codebase (see `close_issue` / `delete_branch`
as the closest analogues).

**`reopen_issue` step placement.** In `_plan_steps()`, the `reopen_issue` step is
inserted **immediately before** the `apply_plan_label` step. This ensures the issue
is open before the label is applied — the order matters because GitHub would still
accept the label write on a closed issue, but the Poller would ignore it.

**`FakeGitProvider` state tracking.** The fake already tracks `closed_issues` as a
`set[tuple[str, int]]`. Rather than adding a parallel `_issue_states` dict, add a
`seed_issue_state(*, project, issue_number, state)` test helper that populates a
new `_issue_states: dict[tuple[str, int], str]` internal dict (defaulting to
`"open"`). `close_issue` continues to record in `closed_issues` (existing tests
assert on this set) and ALSO sets `_issue_states[(project, issue_number)] = "closed"`.
`reopen_issue` sets `_issue_states[(project, issue_number)] = "open"`. `get_issue_state`
reads from `_issue_states`, defaulting to `"open"`. Existing tests are unaffected
because the default is `"open"`.

## Sub-requests (topologically sorted)

1. Add `get_issue_state` and `reopen_issue` to the `GitProvider` Protocol in
   `packages/foreman/src/foreman/v4/git_provider.py`.
2. Add `_issue_states` dict, `seed_issue_state` helper, and implement
   `get_issue_state` + `reopen_issue` in `FakeGitProvider` in the same file.
   Update `close_issue` to write `"closed"` into `_issue_states`.
3. Implement `get_issue_state` and `reopen_issue` in `PyGithubGitProvider`
   in `packages/foreman/src/foreman/v4/pygithub_git_provider.py`.
4. Add delegation methods for `get_issue_state` and `reopen_issue` to
   `RoutingGitProvider` in `packages/foreman/src/foreman/v4/routing_git_provider.py`.
5. Update `ResetPlan` to add `issue_is_closed: bool` and `reopen_issue: bool` fields.
   Update `_discover()` to call `git.get_issue_state()` and accept `force_reopen: bool`.
   Update `_plan_steps()` to emit a `reopen_issue` step before `apply_plan_label` when
   `plan.reopen_issue` is True. Update `_execute()` to handle the `reopen_issue` kind
   by calling `git.reopen_issue()`. Update `cmd_reset()` to accept `--force-reopen` and
   fire the closed-issue guard. All changes in
   `packages/foreman/src/foreman/v4/cli/mutations.py`.
6. Add tests in `packages/foreman/tests/v4/cli/test_mutation_commands.py`.

## File-level changes

| File | Change |
|------|--------|
| `packages/foreman/src/foreman/v4/git_provider.py` | Add `get_issue_state` + `reopen_issue` to `GitProvider` Protocol; add `_issue_states` dict + `seed_issue_state` helper + `get_issue_state` + `reopen_issue` implementations to `FakeGitProvider`; update `FakeGitProvider.close_issue` to also write into `_issue_states`. |
| `packages/foreman/src/foreman/v4/pygithub_git_provider.py` | Add `get_issue_state` (reads `issue.state`) and `reopen_issue` (calls `issue.edit(state="open")` idempotently). |
| `packages/foreman/src/foreman/v4/routing_git_provider.py` | Add `get_issue_state` and `reopen_issue` delegation methods following the existing per-method pattern. |
| `packages/foreman/src/foreman/v4/cli/mutations.py` | Add `issue_is_closed: bool` + `reopen_issue: bool` to `ResetPlan`; add `force_reopen: bool` to `_discover()`; call `git.get_issue_state()` in `_discover()`; add `reopen_issue` step to `_plan_steps()`; add `reopen_issue` executor branch in `_execute()`; add `--force-reopen` option to `cmd_reset()`; add closed-issue guard in `cmd_reset()`. |
| `packages/foreman/tests/v4/cli/test_mutation_commands.py` | Add four tests: closed+no-flag, closed+force-reopen, open unchanged, closed+no-retrigger. |

## Alternatives considered

1. **Auto-reopen on reset (the original approach in the issue body).** Rejected per
   @wrenrichley's comment: a closed issue most often means the work is already done;
   auto-reopening would silently resurrect completed tickets and send the loop to build
   something already merged (observed concretely with foreman#357 / #366).
2. **Silently skip the `foreman:plan` label when the issue is closed (current
   behaviour).** Rejected: silent no-op is the bug being fixed. The operator has no
   indication the re-trigger never happened.
3. **Add a `reopen_issue` flag to the Poller so it rescans closed issues carrying
   `foreman:plan`.** Rejected: changes the Poller's semantics globally and increases
   load. The current "open issues only" invariant is a meaningful scope bound.
4. **`--reopen` instead of `--force-reopen`.** Rejected: `--force-` prefix makes the
   dangerous nature of the operation explicit in the flag name itself, matching the
   error message's framing ("check for a merged PR before re-triggering").

## Open questions

None. Every acceptance criterion traces to a specific file path and method in the
worktree. The flag name (`--force-reopen`) and error message text are specified in
the issue comment.

## Out of scope

- Why issues end up closed-but-not-done (the state desync that created #357's
  condition) — separate concern explicitly called out in the issue body.
- Changing the Poller to scan closed issues.
- Adding any `reopen_issue` audit trail beyond what GitHub's issue timeline already
  provides.
- Migrating or updating any existing test fixtures beyond what is strictly required
  to keep them passing (the `FakeGitProvider` default of `"open"` keeps all existing
  tests unaffected).
