# Foreman Daemon Design

**Status:** Design approved 2026-06-01. Awaiting implementation plan.

**Goal:** Build the orchestrator loop that turns Foreman from a toolkit of four role-runners into an autonomous GitHub-issue-to-PR pipeline. After this lands, an operator tags an issue `foreman:plan` and walks away; the daemon drives the ticket through Planner → Reviewer → (Fixer loop) → spec merge → Worker → Reviewer → (Fixer loop) → impl merge.

**Architecture parent:** [`foreman-v1-architectural-spec.md`](./foreman-v1-architectural-spec.md). This spec is the v1 daemon design — Section 3.1 of the parent ("Polling & daemon loop") expanded with the tactical decisions needed to build it.

**Scope:** v1 daemon only. Multi-WIP concurrency, priority labels, inter-ticket dependencies, and cross-project coordination are explicitly deferred — but v1's design must not foreclose them. Future-proofing decisions are called out where they apply.

---

## 1. Mental model

**Three concurrent loops:**

1. **Poller** — scans configured projects every N seconds (default 30s) for any tickets whose label set differs from the last-known state in SQLite. Anything that changed gets enqueued.
2. **Self-notify hook** — wraps every role run. When a role completes and advances a label, the wrapper enqueues the same ticket immediately so the next stage runs without waiting on the 30s poll.
3. **Worker** — dequeues one ticket at a time, computes which role should run next, runs it, advances label, releases lock. Single worker in v1.

```
┌─────────────────────────────────────────────────────────────────┐
│                    Foreman Daemon                               │
│                                                                 │
│  Poller (30s)                       Self-notify                 │
│      │                                  │                       │
│      │ external changes                 │ internal advances     │
│      ▼                                  ▼                       │
│           ┌──────────────────────────────────┐                  │
│           │   In-memory queue                │                  │
│           │   • de-duped by (project, issue) │                  │
│           │   • sorted on dequeue            │                  │
│           └────────────────┬─────────────────┘                  │
│                            │                                    │
│                            ▼                                    │
│           ┌──────────────────────────────────┐                  │
│           │  Worker — 1 ticket at a time     │                  │
│           │  • acquires per-ticket lock      │                  │
│           │  • computes next_action()        │                  │
│           │  • dispatches to role            │                  │
│           └────────────────┬─────────────────┘                  │
│                            │                                    │
│                            ▼                                    │
│        Planner / Reviewer / Fixer / Worker                      │
│                            │                                    │
└────────────────────────────┼────────────────────────────────────┘
                             │
                             ▼
              GitHub (labels are source of truth)
                             │
                             ▼
           SQLite (`~/.foreman/foreman.sqlite`)
                  audit / replay / reconciliation
```

The key separation: **discovery is independent of execution.** The poller never blocks waiting for the worker. The worker never blocks waiting for the poller. They communicate through the queue.

---

## 2. The queue

**In-memory, single-process, deque-backed.** Not persistent — the SQLite tables provide the replay surface. If the daemon crashes, on restart we reconcile via a full scan (see §6).

**De-duped by `(project, issue_number)`.** If both the poller and the self-notify hook enqueue the same ticket, only one entry exists. Re-enqueueing an already-present ticket updates its `last_enqueued_at` timestamp.

**Sorted on dequeue, not on insert.** Insert is O(1) append. Dequeue is O(n) — scans the queue for the highest-priority item according to:

```
sort_key = (
    -stage_index(ticket.labels),     # higher stage = more advanced; descending
    ticket.last_transition_at,        # older = first; ascending (FIFO tiebreak)
)
```

`stage_index` maps each pipeline stage to an integer:

| Stage (next role to run) | Index |
|---|---|
| Planner | 1 |
| Reviewer on spec PR | 2 |
| Fixer on spec PR | 3 |
| Auto-merge spec PR | 4 |
| Worker (implementation) | 5 |
| Reviewer on impl PR | 6 |
| Fixer on impl PR | 7 |
| Auto-merge impl PR | 8 |

**Why this order:** prefer-further-along, FIFO tiebreak. When 10 tickets are queued at Planner stage, the first to finish Planner becomes most-advanced and continues marching through Reviewer / spec-merge / Worker / impl-merge until it parks or terminates. Only then does the next ticket start. **Result: first ticket merges fast, then second, etc. — soonest value delivered.**

The alternative (FIFO by enqueue time) would lockstep all 10 tickets through each stage — nothing ships for hours, then everything ships at once. Worse latency, worse failure surfacing, worse cache behavior.

**Future:** foreman#22 will add a priority band that wraps this — `(-priority_band, -stage_index, last_transition_at)`. The stage-priority logic remains as the within-band tiebreak.

---

## 3. State machine

A single pure function:

```python
def next_action(ticket: Ticket) -> Action | None:
    """Given a ticket's current state, return the next thing to do.

    Returns None if the ticket is in a wait-state (parked) — the daemon
    skips it until something changes.
    """
    if is_blocked(ticket):
        return None
    return next_role_for(ticket.labels, ticket.project.auto_merge_spec)
```

`is_blocked(ticket)` checks:
- `foreman:hold` present → blocked
- `foreman:failed` present → blocked (human un-labels to retry)
- **(v2 extension point)** any `foreman:blocked-by:#N` label points at a non-terminal ticket → blocked

`next_role_for(labels, auto_merge_spec)` is a pure label-to-action mapping:

| Labels on issue (or spec PR or impl PR) | Action |
|---|---|
| `foreman:plan` | `RunPlanner` |
| `foreman:planning` | `None` (in-flight indicator) |
| `foreman:spec-review` | `RunReviewer(target=spec_pr)` |
| `foreman:spec-fix` | `RunFixer(target=spec_pr)` |
| `foreman:spec-ready` + `auto_merge_spec=true` | `MergeSpecPR` |
| `foreman:spec-ready` + `auto_merge_spec=false` | `None` (await human merge) |
| spec PR merged + issue has `foreman:plan` removed | `RunWorker` |
| `foreman:implementing` | `None` (in-flight indicator) |
| `foreman:impl-review` | `RunReviewer(target=impl_pr)` |
| `foreman:impl-fix` | `RunFixer(target=impl_pr)` |
| `foreman:ready-for-merge` + `auto_merge_impl=true` | `MergeImplPR` |
| `foreman:ready-for-merge` + `auto_merge_impl=false` | `None` (await human merge) |
| impl PR merged + issue closed | `None` (terminal) |

**Purity matters.** This function takes only the ticket's labels and project config; returns only an Action. No I/O, no side effects, no time-dependence. That makes it:
- Trivially unit-testable (no mocks)
- Cheap to call (no API roundtrip)
- The single source of truth for "what should happen next" — any drift between intent and behavior is here, not scattered across the daemon

**v1 simplification:** `is_blocked` only checks `foreman:hold` and `foreman:failed`. v2 adds inter-ticket dependency checks via the same predicate.

### Impl PR base retarget before merge

`MergeImplPR` is not a single `pr.merge()` call. Before merging, the
runner inspects the impl PR's current `base.ref` and retargets it to
the repo's default branch when both of these are true:

1. The impl PR's current base is still the spec branch
   (`foreman/issue-N`).
2. The spec PR for that branch has already merged.

Why: the Worker opens impl PRs with `base=foreman/issue-N` (the
stacked-PR pattern — see `worktree.create_impl`). If the daemon then
calls `pr.merge() + delete_branch=True` without retargeting, GitHub
squashes the impl commits onto the spec branch and immediately deletes
the branch per `--delete-branch`. The squash commit becomes orphaned:
`gh pr view` reports `state: MERGED`, the commit SHA exists, but
`git log main` doesn't include it. The work is silently lost.

This failure mode bit a manual walk on 2026-06-02: foreman#49's
eight-file impl PR ghost-merged onto its spec branch, and recovery
required `git fetch origin refs/pull/N/head:refs/recover/prN` +
`git cherry-pick` into a new PR (#61). The retarget step closes the
same gap for the autonomous path before `auto_merge_impl` is ever
flipped to `True` in production.

The two checks matter in order:

- **Check 1 (base.ref == spec branch)** makes the retarget idempotent.
  The daemon may re-enqueue `MergeImplPR` after a crash; if a prior
  run (or an operator) already retargeted, we skip and go straight to
  merge.
- **Check 2 (spec PR merged)** prevents the opposite failure mode:
  retargeting to `main` when the spec PR is still open would land
  impl changes on `main` that depend on un-landed spec changes,
  breaking the build. The conservative fallback is to merge onto the
  spec branch as before — the impl commits become reachable from the
  spec branch and will land with the spec PR's eventual merge.

The four host adapter methods supporting this step
(`get_pr_base_ref`, `is_pr_merged_for_branch`, `retarget_pr_base`,
`get_default_branch`) are thin PyGithub wrappers on
`GitHubDaemonHost`; the conditional itself lives in
`daemon_runners.merge_impl_pr` where the spec-branch name resolution
(via `foreman.branches.spec_branch`) already lives. See issue #62 for
the original report.

---

## 4. Per-ticket locks

**Acquired before stage-run, released on completion.** Even with `max_concurrent_workers=1` in v1, the lock pattern is in place so v2 can bump worker count without restructuring.

```python
async with ticket_lock(project, issue_number):
    action = next_action(ticket)
    if action is None:
        return  # parked, skip
    result = await dispatch(action, ticket)
    advance_label(ticket, result)
    # self-notify
    if next_action(ticket) is not None:
        queue.enqueue(ticket)
```

Lock implementation: in-process `asyncio.Lock` keyed by `(project, issue_number)`, held in a dict. v2 may need cross-process or cross-machine locks — at that point the lock interface becomes pluggable. For v1, single-process is enough.

**Why this matters now:** if the poller queues a ticket while the worker is mid-stage on the same ticket (race condition), the second worker call will block on the lock until the first releases — at which point the state has advanced and the second call's `next_action` may now return None (correct) or a different action (also correct, because labels are truth). Either way, no double-dispatch.

---

## 5. Polling

**Default interval: 30 seconds. Configurable per-daemon.**

Per cycle, the poller scans each configured project:

```python
for project in config.projects:
    issues = github.search_issues(
        f"repo:{project.repo} is:open label:foreman:*"
    )
    for issue in issues:
        if labels_changed_since_last_seen(issue):
            queue.enqueue(Ticket(project, issue))
            persist_last_seen(issue)
```

The `is:open label:foreman:*` search is the one API call per project per cycle. With 10 projects, that's 10 calls per 30s = 1200/hour, well under the 5000/hour rate limit.

**Forward-compat:** the per-project loop can be parallelized trivially when N projects gets large. Not needed in v1.

**Worktree-side discovery for merged PRs:** the search query catches issues, but merged PRs are how Foreman knows to advance an issue from "spec PR open" to "Worker can start." The poller also runs a per-project query for `is:pr is:merged label:foreman:spec-ready` etc. — finds merged PRs, looks up their linked issue, enqueues that issue.

---

## 6. Crash recovery (full scan on startup)

**On daemon startup:**

1. Load config, open SQLite, init logging.
2. **Full reconciliation poll:** scan every configured project, fetch all `foreman:*`-labeled issues, compare against last-known-state in SQLite.
3. For each ticket whose label set differs from SQLite, enqueue it.
4. **For tickets in in-flight states (`foreman:planning`, `foreman:implementing`, `foreman:spec-fix`-in-progress, `foreman:impl-fix`-in-progress):** add `foreman:failed` and post a comment explaining "daemon crashed mid-run during <role>; remove `foreman:failed` to retry from current state, or operator-replay via `foreman replay <ticket>`."
5. Start poller. Start worker.

**Why halt instead of auto-retry:** roles are not yet guaranteed idempotent. A Planner that died after writing a spec doc but before opening the PR will (on retry) write a second spec doc and open a duplicate PR. A Worker that died after pushing commits but before opening the impl PR will... it depends. Until each role is audited for idempotency, conservative halt-and-notify is the right v1 default.

**v2 path:** make each role idempotent (check "does my expected output already exist?" → resume / no-op / restart). At that point, switch reconciliation policy to auto-retry.

---

## 7. Worktree management

**Cached per ticket. Cleaned on terminal state.**

When the worker starts a stage on a ticket, it checks: does a worktree exist at `~/.foreman/worktrees/<project>/issue-<N>/`?
- **Yes:** reuse it. Fast-path.
- **No:** create it via `WorktreeManager.create()` (existing v1 walking-skeleton code).

When does a worktree get cleaned up?
- Ticket reaches terminal state (impl PR merged + issue closed): clean.
- Ticket goes to `foreman:failed`: leave the worktree alone (human inspection may need it).
- Daemon shutdown: leave worktrees alone (resumption may need them).
- Operator command `foreman worktree clean <ticket>`: explicit cleanup.

**Why caching matters:** the prefer-further-along dequeue means a single ticket may march through 4+ stages without yielding back. Tearing down + recreating the worktree between each stage would be silly. The worktree is the role's working surface — keep it warm while the ticket is active.

---

## 8. Configuration

New `[daemon]` block in `~/.foreman/config.toml`:

```toml
[daemon]
poll_interval_seconds = 30
max_concurrent_workers = 1          # v1 only supports 1; structure tolerates more
log_path = "~/.foreman/daemon.log"   # JSON lines
log_level = "INFO"
sqlite_path = "~/.foreman/foreman.sqlite"
```

New optional fields on `ProjectConfig`:

```toml
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "e:/workspaces/ai/agents/voice"
auto_merge_spec = false             # default
auto_merge_impl = false             # default; will likely default true in v2
```

**Forward-compat tax in v1:** the `max_concurrent_workers` knob exists but only `1` is supported. Validation rejects other values with a clear error.

---

## 9. SQLite storage

`~/.foreman/foreman.sqlite`. Schema:

**`pipelines`** — one row per ticket
| col | type | notes |
|---|---|---|
| `project` | TEXT | from config key |
| `issue_number` | INTEGER | |
| `current_state` | TEXT | derived from labels at last update |
| `started_at` | DATETIME | first time daemon saw it |
| `terminated_at` | DATETIME | nullable; set on merge/close/failed |
| `parent_ticket_id` | INTEGER | nullable; **reserved for v2 inter-ticket deps** |
| `blocks_ticket_id` | INTEGER | nullable; **reserved for v2 inter-ticket deps** |

**`node_runs`** — one row per role invocation
| col | type | notes |
|---|---|---|
| `pipeline_id` | INTEGER | FK to `pipelines` |
| `role` | TEXT | planner/reviewer/fixer/worker |
| `identity` | TEXT | which bot ran |
| `started_at` | DATETIME | |
| `finished_at` | DATETIME | |
| `outcome` | TEXT | success/failure/timeout |
| `structured_output_json` | TEXT | role's structured output (for replay) |

**`transitions`** — one row per label change Foreman applied
| col | type | notes |
|---|---|---|
| `pipeline_id` | INTEGER | FK |
| `at` | DATETIME | |
| `from_labels_json` | TEXT | label set before |
| `to_labels_json` | TEXT | label set after |
| `actor` | TEXT | which role / `daemon-reconciliation` / etc. |

**`failures`** — one row per `foreman:failed` event
| col | type | notes |
|---|---|---|
| `pipeline_id` | INTEGER | FK |
| `at` | DATETIME | |
| `role` | TEXT | which role failed |
| `reason` | TEXT | error class + message |
| `traceback` | TEXT | for debugging |

**`labels_seen`** — last-known label set per ticket, for poller diff
| col | type | notes |
|---|---|---|
| `project` | TEXT | |
| `issue_number` | INTEGER | |
| `labels_json` | TEXT | sorted JSON array of labels |
| `seen_at` | DATETIME | |
| PK | `(project, issue_number)` | |

Migrations: use a simple integer `schema_version` row in a `meta` table. v1 ships schema_version=1. Future bumps run idempotent ALTER TABLEs.

---

## 10. Operator surface

**New commands:**

```
foreman daemon start                    # foreground; honors Ctrl-C
foreman daemon start --detach           # background
foreman daemon stop
foreman daemon status                   # running/stopped, queue depth, in-flight ticket
foreman ps                              # list active tickets + their states + queue position
foreman pipeline-detail <project> <N>   # full audit trail of one ticket
foreman replay <project> <N>            # re-run from a chosen prior state (operator debugging)
foreman worktree clean <project> <N>    # delete worktree (e.g. after manual fixup)
```

**Logging:** JSON lines to `~/.foreman/daemon.log`. One line per:
- Poll cycle start/end + ticket discoveries
- Worker dequeue + dispatch + return
- Label transitions
- Failures (with full traceback)
- Self-notify enqueues

`foreman daemon status` reads the log + SQLite to render a compact summary.

---

## 11. Test strategy

The daemon is async and label-driven; testing it well needs:

- **Pure functions get unit tests with fixed inputs.** `next_action`, `stage_index`, `is_blocked`, dequeue sort key.
- **Role dispatch gets fake-role-runner tests.** Replace the real Planner/Reviewer/etc. with predictable fakes; verify the orchestration logic (label advance, self-notify, lock acquisition) is correct.
- **Poller gets fake-GitHub tests.** Replace `GitHostProvider` with an in-memory fake that returns labeled issues; verify the poller correctly diffs against SQLite and enqueues changes.
- **SQLite migration gets golden-state tests.** Old DB on disk → run migrations → assert new schema + data integrity.
- **Crash recovery gets explicit tests.** Boot daemon with SQLite in various "we crashed during X" states; verify reconciliation produces the right enqueue and label changes.

**End-to-end integration tests** (real GitHub, real bots) are deferred — those become the dogfood runs after the daemon ships.

---

## 12. Decisions deferred (with rationale)

These are real future work. They are NOT promises; they are a list of "known unknowns" so future-us doesn't waste time rediscovering them.

| Decision | Why deferred | Tracking |
|---|---|---|
| Priority labels (`foreman:priority:critical/high/low`) | Need to see real production-bug behavior before designing | foreman#22 |
| Multi-worker concurrency (N>1) | Need per-ticket-lock infra (which v1 builds) + observe queue depths before sizing | (v2) |
| Inter-ticket dependencies (`foreman:blocked-by:#N`) | Need to see how users naturally express deps in real tickets | (v2) |
| Cross-project deps (A waits on B in different repo) | Truly hairy; only matters at scale | (v3+) |
| Webhook-based discovery instead of polling | Polling is cheap enough; need hosted endpoint first | parent spec §3.1 |
| Bus event publishing | Wake subscribers; need wake=false bus capability in agent-core first | parent spec §3.3 |
| Idempotent roles → auto-retry on crash | Need to audit each role for idempotency first | (v2) |
| Token rotation / rate-limit budget management | Single token + 30s poll fits well under limits with 10 projects | (when needed) |
| Daemon hot-reload of config.toml | Restart-on-change is fine for v1 | (v2 nice-to-have) |
| Global bot identities in config | Foreman#17 — separable cleanup | foreman#17 |
| Planner re-run when local branch exists | Foreman#20 — separable robustness | foreman#20 |

---

## 13. Module layout

New files in `packages/foreman/src/foreman/`:

```
daemon.py              # asyncio: poller task + worker task + queue + locks
dispatcher.py          # next_action() pure function + stage_index + is_blocked
storage.py             # SQLite schema, migrations, query helpers
ps.py                  # `foreman ps` / `foreman daemon status` / `foreman pipeline-detail`
```

Extended files:

```
config.py              # +DaemonConfig, +ProjectConfig.auto_merge_spec/impl
cli.py                 # +daemon/ps/pipeline-detail/replay/worktree-clean subcommands
labels.py              # may move next_role_for() here if it's not already
```

---

## 14. Acceptance criteria for v1 daemon

The walking-skeleton-equivalent for the daemon is:

1. `foreman daemon start --detach` runs without crashing.
2. With one project configured and one tagged issue (`foreman:plan`), the daemon runs Planner → spec PR opens. (Auto-merge off → daemon parks; auto-merge on → daemon merges and continues.)
3. With auto-merge on for both spec and impl, a single ticket gets driven from `foreman:plan` to merged impl PR with no operator intervention.
4. With 5+ tickets tagged simultaneously, ticket #1 merges before ticket #2's Planner starts (stage-priority verified).
5. `foreman:hold` on an in-queue ticket prevents new role-runs on it; removing it resumes.
6. `foreman:failed` on a ticket parks it; removing it re-enqueues.
7. SIGTERM during a role run lets it finish, then exits clean.
8. SIGKILL leaves the next startup in a recoverable state (no data corruption, no double-dispatch).

These are the bar to call the daemon "done."

---

## 15. Orchestrator token management

The daemon's orchestrator-bot is a GitHub App identity, same shape as the four role bots (planner, reviewer, fixer, worker). All five mint installation tokens against the same GitHub App protocol — JWT-signed-from-private-key, 1-hour TTL, refresh-on-expiry. The architectural rule is "all GitHub App identities go through `IdentityRegistry`, period." The registry is the canonical token-management seam.

The original walking-skeleton wiring split the orchestrator off into its own one-shot token-mint path in `daemon_host.py` while the role bots routed through `IdentityRegistry`. That split — two parallel token code paths for the same kind of credential — was an accident of how the code grew. It surfaced as the 1-hour-daemon-death bug caught 2026-06-02: a daemon left running overnight became silently inert by morning because the orchestrator's installation token had expired with no refresh path. The role bots inherited the existing 5-minute-pre-expiry refresh logic; the orchestrator did not.

The fix joins the orchestrator role to `_resolve_role_credentials` alongside the four role bots. `IdentityRegistry.__init__` gains a keyword-only `orchestrator: OrchestratorConfig | None = None` parameter; when set, the registry can serve the `"orchestrator"` role. Two convenience accessors — `get_orchestrator_client()` and `get_orchestrator_token()` — mirror the per-role pair. The orchestrator is *global* to one App installation (one set of credentials shared across every repo in the installation), in contrast to the role bots which are per-project; the registry uses the project's repo slug only as the installation-id lookup, and the resulting token spans every repo the App is installed on.

The daemon's `GitHubDaemonHost` takes an `IdentityRegistry` reference at construction time and asks it for a fresh orchestrator client on every API call — `add_issue_label`, `merge_pull_request`, `close_issue`, etc. all open with `gh = self._registry.get_orchestrator_client()`. The registry's cache + refresh sit underneath, so the host never holds stale state and token rollover transparently propagates to the very next API call. This matches how the per-role dispatchers already use the registry (each role run calls `registry.get_<role>_client()` afresh); the same invariant now covers the daemon's host-operation surface too.
