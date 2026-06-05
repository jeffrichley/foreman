# Spec: foreman v3 declarative reconciler (issue #106)

## Goal

Replace foreman v2's stateful pipeline-tracking daemon with a stateless declarative reconciler where GitHub is the sole source of truth for ticket and PR state. The daemon's sqlite database becomes an execution log (a record of what daemon DID, when, with what outcome) — not a parallel state machine that drifts from GitHub.

Tracks issue [#106](https://github.com/jeffrichley/foreman/issues/106). Related: foreman#101 (the stale-pipeline symptom that surfaced the architectural problem), foreman#88 (the sibling "cache vs truth" redesign for the lock+pid files — same fix shape, different layer).

## Why

Foreman v2's daemon was built with sqlite's `pipelines` table holding "intended state" alongside GitHub's actual state. As an optimization (avoid re-querying GitHub each poll), the table started as a cache; over time it drifted into authority. Three concrete failure modes:

- **Externally-closed issues leave `pipelines` rows in `running` status** (zombies). The poller never observes the close because labels on a closed issue don't transition; the worker keeps the row alive forever.
- **Manual PR merges don't propagate to the daemon's label state machine.** v2 polls labels but doesn't watch PR state. A spec PR merged via the GitHub UI never advances `foreman:planning` → `foreman:plan-approved`. The ticket stalls.
- **Crash recovery has to reconcile the parallel state back from GH into the table** on every restart. Recovery code is one of the gnarliest pieces of v2.

As of 2026-06-03: ~6 zombie pipeline rows consume worker capacity; real tickets (e.g. foreman#143) can't dispatch because the queue is full of corpses. The daemon's poll log shows `queue_depth` stable at 2-4 for hours with `changed: 0` every poll — alive but doing nothing useful.

The architectural root cause: when a cache layer and a truth layer hold the same state, they drift. The fix shape is the same as foreman#88's lock-file redesign: make the cache derive from the truth layer, not parallel it. For v3, GitHub is the truth layer. The daemon db holds execution facts (what daemon ran, when, with what outcome) that GitHub doesn't know — not a parallel state machine that mirrors what GitHub already knows.

## Architecture

Single async reconciler loop per poll cycle:

```
1. Fetch GitHub state for all registered projects (one GraphQL query per project)
2. For each open foreman-labeled issue:
   a. Read execution log for "what has daemon done for this ticket?"
   b. Evaluate rule catalog in precedence order → first matching rule emits an action
   c. If action is non-noop AND idempotence check (against log) passes: execute
   d. Write action start + outcome rows to execution log
3. Sleep until next poll
```

No persistent ticket-state table. No queue table. No `pipelines` table. The reconciler's cross-poll memory is exactly one thing: the execution log.

## Components

### `reconciler/observer.py` — GitHub state observer

One GraphQL query per registered project per poll. Returns open issues with any `foreman:*` label and their associated open PRs (joined client-side via `closingIssuesReferences`).

```python
def fetch_project_state(project: Project) -> ProjectSnapshot:
    """Single GraphQL query → ProjectSnapshot{issues, prs, fetched_at}."""
```

`ProjectSnapshot` is an immutable in-memory dataclass — not persisted. The reconciler reads it for the current poll and discards it.

### `reconciler/rules.py` — rule catalog

Ordered list of `(predicate, action)` pairs. First matching rule fires. Two precedence tiers:

- **Safety tier (precedence 0-99)**: surface_help variants. Any safety condition (CI red, mergeable conflict, `foreman:needs-help` label, ticket stale > N hours) preempts forward-progress.
- **Forward-progress tier (precedence 100+)**: dispatch, merge, advance.

A unit test invariant — see Testing Strategy — asserts every safety rule has precedence < the lowest forward-progress rule. Catalog mutations that violate the invariant fail CI.

### `reconciler/exec_log.py` — execution log writer + reader

Append-only sqlite. Single writer (the daemon process). The rule predicates read it for idempotence checks.

### `reconciler/actions.py` — action executor

Maps `Action` enum to a side-effecting function. Each action writes a `_started` log row before executing, then a `_completed` or `_errored` row pointing back at the start row.

### `reconciler/daemon.py` — main loop

Composes observer + rules + actions. Async, 60s poll cadence, graceful shutdown on SIGTERM.

## State boundary

Source-of-truth principle: **GitHub owns ticket state; the daemon db owns execution facts**.

| State | Owned by | Why |
|---|---|---|
| Issue open/closed | GitHub | Truth |
| Issue labels (`foreman:*`) | GitHub | Truth |
| Issue assignee | GitHub | Truth |
| PR state (open/merged/closed) | GitHub | Truth |
| PR mergeable status | GitHub | Truth |
| PR CI status | GitHub | Truth |
| PR → issue linkage | GitHub | Truth (via `closingIssuesReferences`) |
| "Daemon dispatched Planner subprocess at T for ticket X" | Daemon db | Execution fact GH doesn't know |
| "Worker subprocess PID 12345 is currently running for ticket X" | Daemon db | Execution fact GH doesn't know |
| "I last surfaced needs-help for ticket X at T" | Daemon db | Alert rate-limit memory |
| "I last reconciled ticket X at T → rule R fired → action A" | Daemon db | Audit trail |

If a piece of state is in both columns, that's a bug in the design — open a follow-up issue.

## State observation

### Fetch shape

One GraphQL query per registered project per poll. The query returns:

- **Issues**: open issues with any `foreman:*` label — `number, title, state, labels, assignees, body, updatedAt`
- **PRs**: open PRs — `number, state, headRefName, mergeable, body, statusCheckRollup, closingIssuesReferences{nodes{number}}`

PR → issue linkage is joined client-side via `closingIssuesReferences`. Spec PRs vs impl PRs are distinguished by branch name convention (`spec-*` vs `impl-*`) or PR body marker (the existing "Implements #N" convention from foreman#63).

### Cadence

60-second poll interval. Same as v2. Don't change two things at once.

### Rate budget

GraphQL query cost: ~2 points per query (1 base + connection-page cost). 5 projects × 60 polls/hr = 300 queries/hr × 2 points = ~600 points/hr. Auth GraphQL limit is 5000/hr. ~12% utilization. Plenty of headroom.

### Failure handling

- **GH unreachable** (network error): skip this poll cycle, increment failure counter, log warning. After 3 consecutive failures, surface a yellow alert to Wren via bus.
- **GH rate-limited** (403 + quota header): same as unreachable; skip until reset window.
- **GH returns partial / stale data**: trust what comes back. Idempotence of actions absorbs rare flicker.

No degraded "act on stale data" mode. The whole principle of v3 is "GitHub is truth"; acting blind contradicts it.

## Reconciler logic

### Action catalog

| Action | When | What |
|---|---|---|
| `noop` | No matching rule | Nothing |
| `surface_help` | Safety rules fire | Add `foreman:needs-help` label, post issue comment, alert Wren via bus |
| `dispatch_planner` | Ticket has `foreman:planning`, no unterminated planner-started log row | Spawn Planner subprocess on the ticket |
| `merge_spec_pr` | Spec PR exists, CI green, mergeable, ticket has `foreman:planning` | Merge PR via GH API |
| `advance_label_to_plan_approved` | Spec PR merged, ticket still has `foreman:planning` | Remove `foreman:planning`, add `foreman:plan-approved` |
| `dispatch_worker` | Ticket has `foreman:plan-approved`, no unterminated worker-started row | Spawn Worker subprocess on the ticket |
| `dispatch_reviewer` | Impl PR exists, CI green, no review attached, ticket has `foreman:impl-review` | Spawn Reviewer subprocess on the PR |
| `dispatch_fixer` | Impl PR has `foreman:impl-fix` label, no unterminated fixer-started row | Spawn Fixer subprocess on the PR |
| `merge_impl_pr` | Impl PR approved, CI green, mergeable | Merge PR via GH API |
| `advance_label_to_done` | Impl PR merged, ticket still has any open `foreman:*` label | Close issue, add `foreman:done` label, post reference comment |

This catalog mirrors v2's transitions. The point of v3 is not new transitions — it's a sound foundation for them.

### Precedence ordering

Rules evaluate in the order they appear in `RULES`. First matching rule fires; subsequent rules don't evaluate.

- **Safety tier (precedence 0-99)**: any condition that means "this ticket is in a state that needs human attention before any forward-progress is correct" — CI failure, mergeable conflict, `foreman:needs-help` label set manually, ticket stale > N hours mid-transition.
- **Forward-progress tier (precedence 100+)**: dispatch + merge + advance_label actions.

The precedence-ordering invariant test (see Testing) is the safety net. Catalog edits that move a safety rule below a forward-progress rule break CI.

### Idempotence via execution log

Every dispatch and label-advance action consults the execution log before executing:

- `dispatch_planner` checks: any unterminated `planner_started` row for this ticket? → if yes, no-op.
- `dispatch_worker` checks: any unterminated `worker_started` row? → if yes, no-op.
- `advance_label_to_plan_approved` checks: any `label_advanced` row with `from=planning, to=plan_approved` for this ticket? → if yes, no-op.
- `surface_help` checks: any `help_surfaced` row in last hour for this ticket? → if yes, no-op (rate-limit alert noise).

This means double-fires from poll overlap or recovery are safe. The check is one indexed sqlite query, cheap per-action.

## Execution log

### Schema

```sql
CREATE TABLE execution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ticket_id TEXT NOT NULL,         -- "jeffrichley/foreman#143"
    project TEXT NOT NULL,           -- "foreman"
    rule_name TEXT,                  -- which rule fired (NULL for non-rule writes like heartbeats)
    action TEXT NOT NULL,            -- "dispatch_worker" | "merge_spec_pr" | "worker_heartbeat" | ...
    outcome TEXT NOT NULL,           -- "running" | "success" | "error" | "skipped" | "dry_run"
    details JSON,                    -- GH snapshot, PR#/sha, error stack, subprocess PID
    parent_log_id INTEGER REFERENCES execution_log(id)  -- terminations point at their start
);

CREATE INDEX idx_ticket_ts ON execution_log(ticket_id, ts DESC);
CREATE INDEX idx_running ON execution_log(outcome) WHERE outcome = 'running';
```

The partial `running` index makes "is X running?" a 1-row indexed lookup. This is the hot path for idempotence checks — fired ~5 times per poll per ticket — so the partial-index optimization is load-bearing.

### What gets logged

- All state-changing actions: `dispatch_*`, `merge_*`, `advance_*`, `surface_help`
- Worker subprocess heartbeats (every 60s while a subprocess is running)
- Worker subprocess completion / error rows, pointing back at their `_started` row via `parent_log_id`

**Noops are NOT logged.** At ~5 projects × 5 tickets × 60 polls/hr ≈ 1500 evaluations/hr with ~95% noop rate, that's ~1400 noop rows/hr of zero diagnostic value. Per-poll-cycle summaries instead go to the existing `daemon.log` text file ("polled 5 projects, evaluated 12 tickets, emitted 1 action").

### Retention

- **Hot**: 30 days in `execution_log` table
- **Archive**: nightly job (`reconciler.archive_old_rows`) dumps rows older than 30 days to `~/.foreman/archive/execution_log_YYYY-MM.jsonl.gz`
- Hot table stays bounded at ~50k rows under steady traffic

Retention is configurable per-deployment via `DaemonConfig.exec_log_retention_days`. Default 30.

### Single-writer pattern

The daemon process is the **only** writer to `execution_log`. Worker, Planner, Reviewer, Fixer subprocesses do NOT write to the db directly. They communicate progress via the agent-core bus by sending Event envelopes; the daemon receives them and translates to log rows.

Rationale: sqlite multi-writer (via WAL) is technically possible but adds lock contention and complicates testing. Single-writer is simpler. Bus latency overhead is on the order of tens of milliseconds, immaterial compared to the seconds each subprocess action takes.

The daemon exposes a small `reconciler-log` bus endpoint accepting `ExecutionLogWrite` events from subprocesses:

```python
{
    "kind": "Event",
    "type": "ExecutionLogWrite",
    "data": {
        "ticket_id": "jeffrichley/foreman#143",
        "action": "worker_heartbeat",
        "details": {"progress": "ran tests, 8/8 passing"},
    },
}
```

## Migration

### Pre-cutover state (as of 2026-06-03)

v2 daemon already stopped. `~/.foreman/foreman.sqlite` archived to `~/.foreman/foreman-v2-archive-2026-06-03.sqlite`. No active foreman subprocesses. `daemon.lock` + `daemon.pid` cleaned up. v2 is dead; v3 implementation proceeds in calm conditions with no parallel running system to coordinate with.

### Pre-flight gates (must pass before cutover)

1. **Unit tests for the rule catalog**: every rule tested with canned `(issue, pr, log) → expected_action` inputs. Mock `ExecLogReader` with static row lists. ~50-100 test cases covering the catalog.
2. **Precedence-ordering invariant test**: `test_safety_precedes_progress()` walks `RULES`, asserts all safety-tier (precedence < 100) precede all forward-progress (>= 100). Fails CI on violation.
3. **Reconciler integration test**: wires observer + rules + actions with a stub GitHubClient returning canned project state. ~20 scenarios — each known transition + the safety-rule preemptions.
4. **`--dry-run` mode validated locally**: daemon emits intended actions with `outcome="dry_run"` instead of executing.

### Cutover procedure

1. v3 daemon deployed to `~/.foreman/` (fresh db, new schema)
2. Start in `--dry-run` mode: `foreman daemon start --dry-run`
3. Observe first ~6 polls of intended actions in `daemon.log` — gut-check that v3 sees today's stuck tickets and emits the right actions (e.g. for foreman#143: "advance_label_to_plan_approved")
4. Flip to executing mode: stop daemon, restart without `--dry-run`
5. **Tight observation window 24-48h**: Wren on-stream for first day; gut-check actions per cycle; surface anomalies to Jeff

### Rollback plan

If v3 misbehaves:

1. Stop v3 daemon (clean SIGTERM)
2. Rename `~/.foreman/foreman-v2-archive-2026-06-03.sqlite` back to `foreman.sqlite`
3. Restart v2 daemon

Tickets that progressed during v3's brief reign may need manual recovery on the v2 side. Accept this as the escape hatch, not a routine.

### Today's stuck tickets become v3's cutover proof point

v3 derives from GH state at startup. Today's stuck tickets (foreman#143 with merged spec PR but stale `foreman:planning` label, etc.) become v3's first useful actions on day 1: v3 observes "issue open, label `foreman:planning`, merged PR linked → advance_label_to_plan_approved", takes the action, ticket unsticks. **This is the cutover proof point — v3 fixing the exact gum-up that motivated it, in production, in real time.**

## Failure modes & resilience

| Failure | Detection | Response |
|---|---|---|
| GH unreachable / rate-limited | Network error or 403 in observer | Skip cycle; counter increments; yellow alert to Wren after 3 consecutive |
| Rule predicate raises | try/except around predicate eval | Log error, treat predicate as False, continue catalog |
| Action execution returns error (e.g. PR merge fails) | Action wrapper catches | Log error outcome; rule re-fires next poll; after N retries, surface_help |
| Worker subprocess crashes | No completion row, heartbeat stale > 2× cadence | Reconciler emits surface_help next poll |
| Daemon process crashes mid-action | Action row in `running` state with no completion | On restart: scan `outcome='running'` rows, mark each `errored:recovery`, surface_help for affected tickets |
| Bus endpoint receives bad ExecutionLogWrite envelope | Pydantic validation fails | NACK envelope; log error; subprocess can retry |

## Testing strategy

### Unit tests — rule catalog (per-rule)

Each rule tested with canned `(issue, pr, log) → expected_action` inputs. Mock `ExecLogReader` with a static row list. Aim for ~5-10 cases per rule × ~10 rules = 50-100 tests.

### Precedence invariant test

`test_rules_precedence_ordering()`: walks `RULES` list, asserts `all(safety.precedence < progress.precedence for safety in safety_rules for progress in progress_rules)`. One assertion, one test, prevents catalog regressions.

### Reconciler integration test

Single test file that wires observer + rules + actions with a stub `GitHubClient` returning canned snapshots. ~20 scenarios: each known transition + the safety preemptions + the today's-stuck-tickets case.

### Production dry-run

`--dry-run` mode for first ~6 polls of cutover. Observer fetches real GH state; rules evaluate; actions log to `execution_log` with `outcome="dry_run"` instead of executing. Operator inspects log via `sqlite3 ~/.foreman/foreman.sqlite "SELECT * FROM execution_log ORDER BY id DESC LIMIT 20"`.

## Out of scope (explicit)

The following are deliberately NOT in v3 scope:

- **Webhook-based state observation.** Polling is sufficient for current scale; webhooks require a persistent public endpoint + signing + retry queue. Possible v4 evolution.
- **Per-project `v2_or_v3` flag.** Big-bang migration; no parallel-codepath maintenance.
- **Worker process auto-restart on crash.** `surface_help` on detection is the v3 stance; humans intervene. Auto-restart can be a follow-up if recurrence is high.
- **Cross-project dependency awareness.** Each project polled independently; no shared cross-project state.
- **Backfill of v2's `pipelines` table.** Archived but not migrated; v3 derives from GH at startup.
- **A `foreman log <ticket>` CLI for humans.** Defer; raw `sqlite3` queries + `daemon.log` text file suffice early.

## Open questions (genuinely TBD)

- **Daemon process supervision.** Should v3 daemon supervise itself (systemd-on-Windows equivalent for crash-restart)? Defer to a follow-up issue; current manual-start works for now.
- **Wren's role in observation.** Tight 24-48h post-flip observation is human-time. Is that runtime burden acceptable, or should it be automated via "v3 just dispatched action X" Discord alerts on every state change? Defer to post-flip; assess from real signal.
- **Action retry policy details.** After N consecutive errors on the same action for the same ticket, `surface_help` fires — but what's N, and is the retry interval immediate or backoff? Reasonable default: N=3, fire on next poll (no backoff inside one poll). Revisit if it creates alert noise.

## References

- foreman#106 — this epic
- foreman#101 — the stale-pipeline symptom that triggered v3 design
- foreman#88 — sibling "cache vs truth" redesign (the lock-file / pid-file source-of-truth fix)
- foreman#63 — "Implements #N" commit convention (followed by all v3 PRs)
- v2 daemon code: `packages/foreman/src/foreman/daemon/` (deprecated, not deleted, after v3 ships; deletion follow-up after v3 is proven stable for ~2 weeks)
- v2 db archive: `~/.foreman/foreman-v2-archive-2026-06-03.sqlite` (forensic; not migrated)
