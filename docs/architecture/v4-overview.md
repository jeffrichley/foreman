# Foreman v4 — Architecture Overview

**Status:** As-built reference for the current substrate (the v3 reconciler was
deleted in the v4 cutover; see git history for the old `v3-reconciler.md`).
**Purpose:** One current-state map of the v4 daemon — what the moving parts are,
where logic lives, and what the contracts between modules are. PR reviews can
bounce against this; when code drifts from this doc, update the doc in the same
PR.

This is a **reference**, not a tutorial. It assumes you know what foreman does at
the product level (autonomous GitHub-issue → PR loop driven by a daemon-owned
state machine).

---

## 1. Two layers — and why "v4" is only one of them

Foreman is split into two layers that live side by side in the package:

- **`foreman.v4.*` — the orchestration substrate.** The daemon, the state
  machine, the persistence/queue/worker chain, the event bus, and the Protocol
  seams. This is the part the v4 redesign rebuilt. The `v4` namespace is a
  historical marker (it disambiguated from the old reconciler during the
  migration); the old substrate is gone, so the name is now just where the
  orchestration code happens to live.

- **`foreman.*` (top level) — the role-execution layer (the "survival set").**
  The Planner / Reviewer / Fixer / Worker roles and everything they need to run:
  `roles/`, `schemas/`, `prompts/`, `provider(s)`, `auth`, `git_host`,
  `git_hosts/`, `worktree`, `branches`, `stats`, `labels`, `init`,
  `instructions`, `auto_close`, `_env_filter`. This is **permanent, live
  production code** — not residue awaiting deletion. The substrate shells into it
  via subprocess; it is the layer that actually talks to Claude and writes code.

The boundary between the two is mechanically enforced (see §6). The substrate
depends on the role layer (it dispatches into it); the role layer never imports
the substrate.

## 2. Top-level shape

```
                       ┌──────────────────────────┐
                       │   GitHub (source of      │
                       │   truth for issues/PRs)  │
                       └────────────┬─────────────┘
                                    │ PyGithub (REST)
                                    ▼
┌──────────────────────────────────────────────────────────────┐
│  v4 daemon process  (`foreman daemon start`)                   │
│                                                                │
│  Daemon.run_forever                                            │
│    └─ reconcile_on_startup()   (close crash-orphaned rows)     │
│    └─ tick loop (every tick_seconds):                          │
│         Poller(s) ──WorkItem──► QueueManager ──► WorkerPool    │
│                                                    │           │
│                                                    ▼           │
│                            TicketState.transition() (Template) │
│                            ├─ publish events ──► EventBus       │
│                            ├─ journal rows  ──► TicketRepository│
│                            └─ execute       ──► RoleDispatcher  │
└────────────────────────────────────────────────────┬─────────┘
                                                       │ subprocess
                                                       ▼
                                   foreman <role> (survival-set roles)
                                   run in a per-ticket git worktree,
                                   emit FOREMAN_OUTCOME json on stdout
```

## 3. The daemon chain (producer → consumer)

- **`Poller`** (one per project) — adopts new `foreman:plan`-labeled issues and
  re-enqueues every open, non-terminal ticket each tick. It does not advance
  state; it just produces `WorkItem`s. Restart-safety is by re-enqueue: a ticket
  picks up at its persisted `current_state`.
- **`QueueManager`** — priority heap with filters (in-flight / held / dependency
  blocked). Hands out at most `max_in_flight` items at a time.
- **`WorkerPool`** — a `ThreadPoolExecutor` sized to `max_in_flight` (the
  GLOBAL cap, total tickets in flight across all repos). Each slot runs one
  `TicketState.transition()`. **Serial *per repo*** — every repo is pinned to a
  per-repo cap of 1 (`ProjectConfig.max_in_flight`), so DIFFERENT repos run in
  parallel (one foreman + one agent_core ticket at once) while each repo stays
  serial (see §8).

## 4. The state machine

`TicketState.transition()` (`v4/states/state.py`) is a **Template Method**:
`can_run → cap-check → enter → execute → verify → next_state → exit`, with
per-phase failure handling and a `finally` block that always closes the journal
row and emits a `StateExitedEvent`.

States are registered in `STATE_REGISTRY` (name → factory). The lifecycle:

```
Queued → Planning → SpecReview → SpecMerging → Implementing
       → ImplReview → ImplApproved ──(human merges impl PR)──► Merging → Done
                          │
       (any phase) ───────┴──────► NeedsHelp / Failed (terminal, human-actioned)
```

- **Role-dispatch states** (Planning, SpecReview/SpecFix, Implementing,
  ImplReview/ImplFix) shell out to a role subprocess and route on the returned
  `Outcome`.
- **Merge states** (SpecMerging, Merging) re-derive from GitHub
  (`get_pr_state`) and treat already-merged as success, so they are
  crash-idempotent.
- **ImplApproved** polls each tick for the human to merge the impl PR, then
  finalizes (closes the originating issue) and advances to Done.

## 5. Seams (Protocols + fakes)

Every external dependency is a `typing.Protocol` with an in-memory fake held to
behavioral parity by a shared contract suite:

| Protocol | Real impl | What it owns |
|---|---|---|
| `TicketRepository` | `PostgresTicketRepository` | the `tickets` / `state_instances` / `events` journal |
| `GitProvider` | `PyGithubGitProvider` (via `RoutingGitProvider`, one per project) | labels, PR state, merge, issue comments |
| `RoleDispatcher` | `SubprocessRoleDispatcher` | fork a role subprocess, parse `FOREMAN_OUTCOME` |

PyGithub never leaks past `GitProvider`; psycopg never leaks past
`PostgresTicketRepository`. Vendor exceptions are translated at the seam
(`GithubException → PRNotFoundError`, `UniqueViolation → TicketAlreadyExistsError`).

**Storage is Postgres in production.** `bootstrap` is Postgres-only (the config
validator requires a DSN); there is no silent SQLite fallback.

## 6. Crash recovery

`reconcile_on_startup()` (`v4/reconcile.py`) runs before the first tick. It calls
`list_in_flight_state_instances()` and closes every row a dead process left open,
recording `failure_phase = "crash_recovery"`. That phase is exempt from the
runaway-cap counters (`count_consecutive_same_state` etc.), so a restart doesn't
push a ticket toward a false NeedsHelp escalation. Role re-dispatch after a crash
resumes the prior Claude session where possible (`v4/states/resolve_dispatch.py`)
rather than re-running fresh.

## 7. Observability + boundaries

- **`EventBus`** (synchronous, exception-isolated) fans every transition out to
  observers: `StructuredLog`, `EventArchive`, `LabelObservability`, `Metrics`,
  `SustainedBlocked`, `TerminalLanding`. An observer that raises never fails a
  transition.
- **Import-linter contracts** (`pyproject.toml`):
  - **R1** — production code never imports tests.
  - **R2** — `foreman.v4.*` never imports the (deleted) v3-substrate modules. The
    forbidden list names modules that no longer exist; it is a permanent
    anti-resurrection guard, not a temporary fence.

## 8. Known tradeoffs (current, deliberate)

- **Serial per repo, parallel across repos** — the GLOBAL `max_in_flight`
  (default 4) is a total-across-repos ceiling; the per-repo cap
  (`ProjectConfig.max_in_flight`, pinned to 1) keeps each repo serial. This
  buys the correctness the old global `= 1` pin bought — no *same-repo*
  concurrent-ticket or merge/rebase race — while recovering cross-project
  throughput (foreman and agent_core advance simultaneously). The remaining
  limit is *intra*-repo: >1 ticket in the SAME repo is still unsafe (a second
  merge could leave the first PR BEHIND its base mid-flight). Lifting the
  per-repo cap above 1 needs the merge-coordinator work (foreman#316 / the
  self-heal review's ADR-0) — a per-repo FIFO merge queue — first.
- **Synchronous event bus** — deterministic observer order; a slow observer (one
  doing a GitHub round-trip) runs inline on the transition hot path. Fine at
  current label volume.

See `docs/architecture-review-2026-06-25.md` and
`…-independent.md` for the full quality-attribute analysis behind these.
