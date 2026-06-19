# `foreman reset` CLI — Design

**Date:** 2026-06-17
**Status:** Brainstorm-validated. Ready for implementation plan.

## Goal

Add a `foreman reset` CLI command that fully wipes the state of a single foreman ticket — labels, remote branches, open PRs, local worktrees, and the SQLite row — and re-triggers the autonomous loop. Operator lever for the recurring stale-state-after-failure case.

## Motivation

Three dogfood failures in two weeks (algokit#21, algokit#23, agent_core#180 ×2) have all been blocked by the same class of bug: a prior pipeline attempt leaves debris (remote branches, local worktrees, SQLite rows), and the naive retry path doesn't validate freshness before short-circuiting. The mechanical recovery is well-trodden:

1. Strip `foreman:*` labels from the issue
2. Delete remote `foreman/issue-N` + `foreman/impl-N` branches
3. `rm -rf` `~/.foreman/worktrees/<project>/issue-N/` + `impl-N/`
4. `DELETE FROM tickets WHERE id = ?` (manual, because `foreman drop` doesn't actually delete the row — see below)
5. Re-apply `foreman:plan` so the daemon picks the issue up fresh on next poll

Doing this by hand each time is friction. The pattern is stable enough to encode.

The underlying substrate bugs (`foreman drop` not deleting the row, WorktreeManager.attach short-circuiting on stale paths) are tracked as the Phase 9 carry-forward in foreman#319. Those fixes go after the substrate cutover; this command is the operator-side stopgap that makes the carry-forward survivable.

## Architecture

`cmd_reset` lives in `packages/foreman/src/foreman/v4/cli/mutations.py` next to its sibling operator commands (`hold`, `resume`, `retry`, `skip`, `drop`, `set-state`, `enqueue`). Two-phase shape: **discover** (read-only) builds a `ResetPlan` dataclass; **execute** walks the plan with no further GitHub or filesystem queries.

The command relies on five new methods added to three existing substrate seams:

- `GitProvider.delete_branch`
- `GitProvider.close_pr`
- `GitProvider.find_open_pr_by_head_branch`
- `TicketRepository.delete_ticket`
- `WorktreeManager.prune`

Each method is idempotent on missing-target — the discovery phase only adds a step to the plan when the target actually exists, and execution swallows "already gone" errors gracefully. This makes reset safely re-runnable on a partially-clean state.

## CLI surface

```
foreman reset --project <name> --issue-number <N> [flags]

Flags:
  --keep-pr           Don't close open spec/impl PRs (default: close them)
  --keep-worktree     Don't rmtree local worktrees (default: prune them)
  --no-retrigger      Don't re-apply foreman:plan at the end (default: re-apply)
  --dry-run           Print plan, exit. No prompt, no execution.
  --yes               Skip the interactive confirmation.
```

Examples:

```sh
# typical: full wipe + re-trigger, with prompt
foreman reset --project agent_core --issue-number 180

# CI / scripted: same, no prompt
foreman reset --project agent_core --issue-number 180 --yes

# inspect first
foreman reset --project agent_core --issue-number 180 --dry-run

# clean state but inspect planner output before re-triggering
foreman reset --project agent_core --issue-number 180 --keep-worktree --no-retrigger
```

### Why `--project` + `--issue-number` instead of `<ticket_id>`

The other operator commands take `<ticket_id>` because they operate on the row. Reset must work even when the row has already been deleted (which is the most common path into "I want to reset"). Using `--project` + `--issue-number` lets reset address debris that has no corresponding row. The command internally resolves the ticket id via `repo.get_ticket_by_issue(...)` when the row exists.

### Why `--yes` instead of always-on confirmation

Default is interactive prompt because reset is destructive (deletes branches, closes PRs, drops the row). `--yes` is the escape hatch for scripted use. `--dry-run` is for inspecting the plan without executing or being prompted.

## Plan-and-confirm flow

`foreman reset --project agent_core --issue-number 180` discovery output:

```
Resetting agent_core#180:

  1. Close PR #19 (spec, "draft: planner output for #180")
  2. Close PR #21 (impl, "feat: discord reaction gate")
  3. Delete remote branch foreman/issue-180
  4. Delete remote branch foreman/impl-180
  5. Prune local worktree ~/.foreman/worktrees/agent_core/issue-180/
  6. Prune local worktree ~/.foreman/worktrees/agent_core/impl-180/
  7. Strip 3 foreman:* labels from issue #180
       (foreman:state-failed, foreman:merging-plan, foreman:impl-approved)
  8. Delete ticket row id=5 from state.db
  9. Apply foreman:plan label to issue #180

Proceed? [y/N]:
```

Discovery surfaces only what actually exists. Missing pieces are absent from the plan (no impl PR → no step 2, no impl branch → no step 4, no ticket row → no step 8). Reset is always idempotent: re-running on an already-clean issue produces a near-empty plan (likely just step 9 if `--no-retrigger` isn't set).

Execution prints each step's status as it goes:

```
  [1/9] Closing PR #19 ............ ok
  [2/9] Closing PR #21 ............ ok
  ...
  [9/9] Applying foreman:plan ..... ok

Done. agent_core#180 reset. Daemon will pick up on next poll (~30s).
```

`--dry-run` prints the plan, exits 0. `--yes` prints the plan + executes without prompting.

## Substrate additions

### GitProvider

Three new methods. Land in Protocol + FakeGitProvider + RoutingGitProvider + PyGithubGitProvider.

```python
def delete_branch(self, *, project: str, branch_name: str) -> None: ...
    # Idempotent: 404 (branch doesn't exist) is a no-op.

def close_pr(self, *, project: str, pr_number: int) -> None: ...
    # Close without merging. Idempotent: already-closed is a no-op.
    # Distinct from close_issue — PyGithub treats PRs and issues separately.

def find_open_pr_by_head_branch(
    self, *, project: str, branch_name: str,
) -> int | None: ...
    # Returns PR number or None. Used by reset's discovery phase to
    # locate spec/impl PRs without depending on the SQLite ticket row.
```

PyGithub mechanics:

- `delete_branch`: `repo.get_git_ref(f"heads/{branch_name}").delete()`, catching `GithubException` with status 404 / 422 (already-deleted).
- `close_pr`: `repo.get_pull(pr_number).edit(state="closed")`. Already-closed = no-op at the API level.
- `find_open_pr_by_head_branch`: `repo.get_pulls(state="open", head=f"{owner}:{branch_name}")`. Returns first match's number or None.

FakeGitProvider implementation: in-memory dicts for branches + PRs, plus recorder sets for assertion in tests.

### TicketRepository

```python
def delete_ticket(self, ticket_id: int) -> None: ...
    # Cascade: also deletes all state_instances rows for this ticket.
    # Raises TicketNotFoundError if the ticket doesn't exist (caller's
    # responsibility to swallow if idempotency is desired).
```

SQLite impl: implementation plan should first check `packages/foreman/src/foreman/v4/schema.sql` for an existing `ON DELETE CASCADE` declaration on `state_instances.ticket_id`. If present, a single `DELETE FROM tickets WHERE id = ?` suffices. If absent, do an explicit pre-step `DELETE FROM state_instances WHERE ticket_id = ?` then `DELETE FROM tickets WHERE id = ?` inside one transaction. Either way, foreign-key enforcement (`PRAGMA foreign_keys = ON`) must be active for cascade to fire.

InMemory impl: remove from `_tickets`, `_by_issue`, and every `_instances` entry with matching `ticket_id`. Raises `TicketNotFoundError` if the id isn't present in `_tickets`.

Lands in the shared `_repository_contract.py` so both impls are kept honest.

### WorktreeManager

```python
def prune(self, *, project: str, issue_number: int) -> list[Path]: ...
    # Removes both ~/.foreman/worktrees/<project>/issue-N/ and impl-N/
    # via `git worktree remove --force` (or rmtree fallback). Returns
    # the list of paths actually removed. Missing dirs are a silent skip.
```

For each of the two target paths:

1. If the path is a registered git worktree, run `git worktree remove --force <path>` from the source clone (per `V4Config.projects[*].local_clone_path`).
2. If the worktree-remove fails or the path isn't a registered worktree, fall back to `shutil.rmtree(path, ignore_errors=False)`.
3. Skip paths that don't exist (return without appending to result list).

## `cmd_reset` orchestration

Plan dataclass:

```python
@dataclass(frozen=True, slots=True)
class ResetPlan:
    project: str
    issue_number: int
    spec_pr: int | None        # discovered via find_open_pr_by_head_branch
    impl_pr: int | None
    delete_branches: list[str] # ["foreman/issue-180", "foreman/impl-180"]
    prune_worktrees: bool      # WorktreeManager.prune is all-or-nothing per ticket
    strip_labels: set[str]     # foreman:* labels currently on the issue
    delete_ticket_id: int | None  # ticket row id, or None if no row
    apply_plan_label: bool
```

Flow:

```
1. Discover  → build ResetPlan from current GitHub + SQLite + filesystem state
2. Render    → print numbered plan (respects --keep-* / --no-retrigger flags
               by omitting the corresponding steps from the rendered plan)
3. Gate      → if --dry-run, exit 0; if not --yes, prompt "Proceed? [y/N]"
4. Execute   → walk plan, printing [N/M] step status as each completes
```

**Discovery phase** (read-only):

```python
def _discover(
    git: GitProvider,
    repo: TicketRepository,
    project: str,
    issue_number: int,
    keep_pr: bool,
    keep_worktree: bool,
    retrigger: bool,
) -> ResetPlan:
    spec_pr = None if keep_pr else git.find_open_pr_by_head_branch(
        project=project, branch_name=f"foreman/issue-{issue_number}",
    )
    impl_pr = None if keep_pr else git.find_open_pr_by_head_branch(
        project=project, branch_name=f"foreman/impl-{issue_number}",
    )
    delete_branches = [
        f"foreman/issue-{issue_number}",
        f"foreman/impl-{issue_number}",
    ]
    labels_on_issue = git.get_issue_labels(project=project, issue_number=issue_number)
    strip = {lbl for lbl in labels_on_issue if lbl.startswith("foreman:")}
    try:
        ticket = repo.get_ticket_by_issue(project=project, issue_number=issue_number)
        delete_ticket_id = ticket.id
    except TicketNotFoundError:
        delete_ticket_id = None
    return ResetPlan(
        project=project,
        issue_number=issue_number,
        spec_pr=spec_pr,
        impl_pr=impl_pr,
        delete_branches=delete_branches,
        prune_worktrees=not keep_worktree,
        strip_labels=strip,
        delete_ticket_id=delete_ticket_id,
        apply_plan_label=retrigger,
    )
```

**Execute phase:** walks the plan, each step in its own try/except. Per-step failure prints `fail: <reason>` but the orchestrator continues to the next step. At end, exit code 0 if all steps succeeded, 1 if any failed (with summary: "completed 7/9 steps; 2 failed"). Rationale: a partial reset is more useful than a halt — the operator can re-run reset (idempotent) to clean up residue.

The plan renderer (printing the numbered list) is a small private helper in `mutations.py`. No new module yet — promote if reset's logic grows beyond ~150 lines.

## Testing strategy

Follows the existing v4 patterns: shared contract suites for substrate seams, fake-backed unit tests for orchestration. No real-fork integration test (the cmd is exercised manually on agent_core#180 right after merge — same shape as the Phase 8d.10 `enqueue` validation).

**Substrate seam tests:**

- `_repository_contract.py` — add `delete_ticket` cases (happy path, cascade-deletes-state-instances, raises on missing ticket). Runs against both InMemory and SQLite via the shared contract harness, catching impl drift.
- `test_git_provider_fake.py` — `delete_branch` (idempotent on missing), `close_pr` (idempotent on already-closed), `find_open_pr_by_head_branch` (returns None when no PR matches).
- `test_pygithub_git_provider.py` — same three methods against the real PyGithub seam with mocked Github client.
- `test_routing_git_provider.py` — confirms multi-project routing dispatches the three new methods to the correct per-project provider.
- New `test_worktree_manager_prune.py` — exercises `prune` against a temp dir with both `issue-N` + `impl-N` subdirs, asserts both are removed and the returned path list matches.

**`cmd_reset` unit tests** (in `test_mutation_commands.py` next to siblings):

- Happy path: full wipe, asserts the printed plan matches expected, all 9 steps execute, exit 0.
- `--dry-run`: plan printed, no mutations occur on the fake providers/repo.
- `--yes`: no prompt, plan printed once, executes.
- `--keep-pr` / `--keep-worktree` / `--no-retrigger`: each suppresses the relevant step in plan + execution.
- Idempotency: run reset twice in a row, second run produces a near-empty plan.
- Partial failure: one step fails, others continue, exit 1, summary line correct.
- No-row case: when no ticket row exists but debris does, plan still cleans the debris.

All `cmd_reset` tests use the existing `CliContext` + `FakeGitProvider` + `InMemoryTicketRepository` + a temp-dir-backed `WorktreeManager`. No subprocess, no real GitHub.

## Out of scope

- Bulk reset (`foreman reset --all-failed` or similar) — easy to add later if the pattern emerges; not needed for the dogfood.
- Reset preserving the SQLite ticket row's audit history (state_instances) — the current design cascade-deletes everything. If audit retention becomes needed, add a `--keep-audit` flag in a follow-up.
- Fixing the substrate bugs the reset command papers over (`foreman drop` not actually deleting the row, WorktreeManager.attach not validating freshness). Those are foreman#319's carry-forward scope.
- A `foreman gc` orphan-debris sweep that walks the filesystem + GitHub looking for stale branches/worktrees not tied to any open ticket. Defer until the dogfood produces evidence we need it.

## References

- foreman#319 — Phase 9 epic with carry-forward bugs that motivate this command
- foreman#318 — Dead-code removal blocking #319
- `packages/foreman/src/foreman/v4/cli/mutations.py` — sibling operator commands
- `packages/foreman/src/foreman/v4/git_provider.py` — GitProvider Protocol
- `packages/foreman/src/foreman/v4/repository.py` — TicketRepository Protocol
- Phase 8d.10 `enqueue` command — pattern for direct-SQLite operator-level mutations
