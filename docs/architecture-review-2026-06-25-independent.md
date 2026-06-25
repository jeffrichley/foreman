# Architecture Review: foreman v4 — orchestration core + autonomous loop

**Reviewer:** architecture-review skill (independent pass) · **Date:** 2026-06-25
**Scope:** `packages/foreman/src/foreman/v4/` — the v4 state machine, daemon tick-loop, queue/worker pool, repository layer, event bus, subprocess role dispatcher, observers, config + bootstrap. Residual v3 surface (`foreman/*` outside `v4/`) in scope only where v4 depends on it.
**System profile:** Long-running daemon · single process · multi-project GitHub-issue→PR orchestrator dispatching role subprocesses (Planner/Reviewer/Fixer/Worker) · low ticket-volume · internally operated · Python 3.12 · PyGithub / psycopg / claude-agent-sdk · Postgres in production (SQLite also present). Entry point: `foreman = "foreman.v4.cli:main"`.

## Executive summary

foreman v4 is a well-structured, seam-disciplined daemon. Its spine — a Template-Method state machine (`TicketState.transition`) over a persisted journal (`state_instances`), fed by a `Poller → QueueManager → WorkerPool` producer/consumer chain, with all I/O behind Protocols (`TicketRepository`, `GitProvider`, `RoleDispatcher`) — is genuinely good architecture. Third-party containment is strong: PyGithub never leaks past the `GitProvider` seam, and a mechanically-enforced import-linter contract (R2) keeps v4 off the dead v3 substrate (both contracts verified KEPT). The subprocess dispatcher's resource lifecycle is unusually careful.

The headline weakness is **crash recovery**, and it is the one area where the system's chosen design (durability via a re-derivable journal + stateless restart) does not fully deliver. The daemon has **no startup reconciliation**: `list_in_flight_state_instances()` exists on the Protocol and both repository impls but has **zero callers** in production code. The practical consequence is a real, latent bug (C1): an orphaned in-flight `state_instances` row left by a crash mid-`execute()` is *counted* by the runaway-defense logic, so a few crash/restart cycles on the same ticket silently escalate it to NeedsHelp despite no genuine failure — and the orphan rows are never closed, leaking. The second-order concern (I1) is that role-subprocess re-execution after a crash relies on role-side idempotency that is explicitly deferred (spec C3), so a crash after a side-effect but before the journal advance can double-act on GitHub for the role-dispatch states (the merge states are safe because they re-derive from GitHub state).

The single most important *tradeoff* the system has made — and mostly made well — is **single-`max_in_flight` simplicity vs. concurrency/throughput**: one knob sizes both the queue cap and the thread pool, and several correctness properties (the merge/rebase race in `MergingState`, the single shared SQLite connection under one RLock) currently lean on `max_in_flight = 1`. That is the right call for a low-volume internal tool today, but the constraint is implicit and under-enforced, and raising the knob is more dangerous than it looks.

## Quality-attribute scorecard

Scale: 1 = structural rework needed · 3 = sound with notable gaps · 5 = exemplary.

| Attribute | Score | One-line justification |
|---|---|---|
| Reliability (crash recovery / idempotency) | **2/5** | No startup reconciliation; orphaned in-flight rows mis-count toward the runaway cap (C1) and leak; role re-dispatch idempotency deferred (I1). |
| Modifiability | **4/5** | Adding a state or role is a localized, well-documented edit (registry + one class + one dispatcher entry); seams are clean. Slightly dragged by config-default sprawl (I3). |
| Testability | **5/5** | Every external dependency behind a Protocol with a contract-tested fake; the same suite runs against InMemory/SQLite/Postgres. Exemplary seams. |
| Observability | **4/5** | Rich event bus + structured-log/label/metrics/escalation observers, exception-isolated. Gap: silent worker-future crashes are only logged, not surfaced as state (I2). |
| Operability | **3/5** | Good CLI surface, PID-file lifecycle, backups, doctor. But `reload` only reloads logging (not config, despite README claim), and recovery from a crash is manual. |
| Availability | **3/5** | Single process, no built-in restart supervision (delegated to Docker/tini — reasonable); a single stuck child is bounded; but a stuck reader-thread / 5s drain budget can briefly wedge a worker. |
| Security | **4/5** | Per-role GitHub App tokens (least privilege), short-lived installation tokens with refresh, no PATs, config validated at the boundary. Token injected via subprocess env (acceptable). |

## Tradeoff points (the headline)

> The decisions where two qualities genuinely conflict. These are the most important output of the review.

1. **One `max_in_flight` knob sizes both the QM cap and the thread pool, and correctness leans on it being `1`.** Trades **operability/simplicity** (operators dial exactly one number; pool and queue can never disagree — `worker_pool.py:75`, `config.py:316`) against **availability/throughput and concurrency-correctness**. The simplicity is real and well-argued. But `MergingState` documents that the merge/rebase race is "operationally sidestepped by `max_in_flight = 1`" (`states/merging.py:35`), and the SQLite repository serializes *all* access through a single connection + one `RLock` (`sqlite_repository.py:129–135`) — so raising the knob both re-opens a correctness gap and converts the DB into a global-mutex bottleneck. **My read:** correct default for a low-volume internal tool, but the dependency is implicit. The `1` should be enforced or guarded, not just commented (see C1/I4). Postgres already removes the DB-contention half of this; the merge-race half does not yet have a code-level guard.

2. **Durability-by-journal + stateless restart vs. actual crash recovery.** Trades **modifiability/simplicity** (no reconciliation logic; the Poller just re-enqueues by `current_state` and the role "idempotency takes care of duplication" — `poller.py:9–16`) against **reliability** (orphaned rows, mis-counted retries, possible double side-effects). The design *intends* to be restart-safe, and for the merge states it is (they re-derive from GitHub). But the intent isn't realized for the journal's own bookkeeping (C1) or for role side-effects (I1). **My read:** the architecture is one small reconciliation pass away from matching its own design intent; right now the gap is silent.

3. **Synchronous event bus vs. observer latency on the transition hot path.** Trades **simplicity/predictability** (deterministic observer order, no thread-safety debt — `event_bus.py:5–12`) against **availability** (a slow observer — e.g. `LabelObservabilityObserver` doing a GitHub round-trip — runs inline inside `transition()` and lengthens the time a worker holds its slot). Exception isolation is handled well (`event_bus.py:44–89`); *latency* isolation is not. **My read:** fine at current scale and label volume; worth a note for when observer count or per-observer I/O grows. This is a sensitivity point for availability, not yet a risk.

## Critical findings (will bite in production or blocks evolution)

### C1. No startup reconciliation — crash-orphaned in-flight rows are counted as failures and leak.

- **What:** When the daemon crashes mid-transition, `open_state_instance()` has already written a `state_instances` row (`worker_pool.py:122–130`) with `failure_phase = NULL`, `outcome_kind = NULL`, `state_name = current_state`, `exited_at = NULL`. Nothing ever closes or reconciles it. On restart the Poller re-enqueues the ticket by `current_state` (`poller.py:84–103`), a *new* row is opened, and the orphan persists. Crucially, `count_consecutive_same_state()` skips only `failure_phase == 'can_run'`, `BLOCKED`, and `TRANSIENT_PROVIDER_ERROR` rows (`sqlite_repository.py` `count_consecutive_same_state`; `repository.py:428–461`; mirrored in `postgres_repository.py:456–475`). A crash-orphan matches **none** of those skips, so it **counts**. After `max_state_attempts` (default 3) crash/restart cycles on the same state, `transition()` escalates the ticket to NeedsHelp (`state.py:251–282`) even though no role ever genuinely failed. Separately, the orphan rows accumulate forever (they're never `close_state_instance`'d), which is the exact leak class the code elsewhere works hard to avoid (see the F4 comment at `state.py:218–222`).
- **Evidence:** `list_in_flight_state_instances` defined at `repository.py:83`, `repository.py:375`, `sqlite_repository.py:357`, `postgres_repository.py:376` — and **called nowhere** in `foreman/v4/` production code (verified by grep; only the three definitions match). Counting logic: `repository.py:428–461`. Restart re-enqueue: `poller.py:84–103`. Cap escalation: `state.py:251–282`.
- **Why it matters:** Reliability + availability. A daemon restart (deploy, OOM, host reboot — all normal for a long-running service) is silently converted into progress toward a false NeedsHelp escalation, and the journal grows unbounded with orphan rows that also distort every consecutive-count query. This is the single biggest gap between the system's *intended* restart-safety and its *actual* behavior.
- **Recommendation:** Add a one-pass `reconcile_on_startup()` invoked from `bootstrap_cli_context` (or at the top of `Daemon.run_forever`) that calls `list_in_flight_state_instances()` and, for each orphan, records a synthetic failure (`failure_phase = "crash_recovery"`, a distinct reason) and closes the row via `close_state_instance`. Then add `failure_phase == "crash_recovery"` to the skip set in both `count_consecutive_same_state` and `count_consecutive_transient_provider_errors` so recovered crashes don't count toward either cap. This is small, uses primitives that already exist, and makes the restart story match the design intent. Record as an ADR (below) since "how a crash is reconciled" is a genuine design decision.

## Important findings (fix before it compounds)

### I1. Role-dispatch re-execution after a crash relies on idempotency that is explicitly deferred.

- **What:** For the role-dispatch states (`PlanningState`, `ImplementingState`, the Fixer/Reviewer states), `execute()` shells out to a subprocess that creates branches, opens PRs, and posts comments (`states/role_dispatch.py:30–45`). If the daemon crashes after the subprocess produced GitHub side-effects but before `mark_execute_completed` + `set_ticket_state` commit (`state.py:320–335`), the restart re-runs the *entire* role from scratch. The Poller docstring concedes this: visible-to-GitHub duplication is left to "the role's idempotency … (deferred per spec C3)" (`poller.py:9–16`). The merge states are *not* exposed to this — `attempt_merge` re-derives from `get_pr_state` and treats `merged=True` as CLEAN (`states/merge_helper.py:130–145`), so they are crash-idempotent. The role states are the gap.
- **Evidence:** `poller.py:9–16`; `states/role_dispatch.py:30–45`; ordering in `state.py:296–341`.
- **Why it matters:** Reliability. A mistimed crash can yield duplicate spec PRs or duplicate comments. Low-volume operation makes this rare, but "rare and silent" is the worst failure shape for an autonomous loop a human only spot-checks.
- **Recommendation:** Two complementary options, pick per appetite: (a) thread a stable idempotency key into the role subprocess (the code already passes `FOREMAN_STATE_INSTANCE_ID` for dedup — `subprocess_dispatcher.py:247–248`; extend that contract so roles *must* no-op on a repeat key); and/or (b) have the C1 reconciliation pass mark crash-orphaned role-dispatch rows in a way the next dispatch can detect ("a prior attempt may have partially acted — verify before re-acting"). At minimum, promote the deferred C3 note from a docstring aside to a tracked, visible risk.

### I2. Worker-thread crashes are logged but never reflected in ticket/journal state.

- **What:** `_run_transition` runs inside a `ThreadPoolExecutor` future. If it raises *before* `transition()` can record a failure (e.g. `get_ticket` raises `TicketNotFoundError`, or `open_state_instance` raises) the exception is captured by the future and only surfaced via `_log_exception` to a logger (`worker_pool.py:111–120`). The QM slot is freed (`mark_done`), the ticket's `current_state` is unchanged, and the Poller will re-enqueue it next tick — potentially forever, with the only evidence buried in logs and nothing on the journal or the GitHub issue.
- **Evidence:** `worker_pool.py:79–120` (callbacks + `_log_exception`); `_run_transition` body `worker_pool.py:122–143`.
- **Why it matters:** Observability + reliability. A class of failures (anything that breaks *before* the Template Method's own try/except scopes) is invisible to operators except in raw logs, and self-perpetuates via re-enqueue.
- **Recommendation:** In the `_on_done_log` callback, on a non-None future exception, record a failure against the ticket (open+fail+close a synthetic instance, or set a `crash`/`NeedsHelp` state) so the loop doesn't silently re-attempt and an operator sees it on the issue. Keep the log line too.

### I3. Config defaults and "skip the cap"/exemption rules are spread across many sites — single-source-of-truth drift risk.

- **What:** The `max_state_attempts = 3` default appears independently in `DaemonConfig` (`daemon.py:62`), `StateContext` (`state.py:63`), `WorkerPool` (`worker_pool.py:53`), and `V4Config` (`config.py:319`), each with a comment explaining why it's `3` "to keep test fixtures green." Likewise the runaway-cap *exemption* rules (`can_run` / `BLOCKED` / `TRANSIENT_PROVIDER_ERROR` skips) are hand-duplicated across three repository implementations (`repository.py:428–488`, `sqlite_repository.py` counts, `postgres_repository.py:456–493`). The contract tests guard behavioral parity, but the *policy* (which outcome kinds are runaway-exempt) lives in three copies and must be edited in lockstep.
- **Evidence:** default sprawl cited above; triplicated skip logic in the three repos.
- **Why it matters:** Modifiability. Adding a new runaway-exempt outcome kind (the obvious future change — e.g. C1's `crash_recovery`) is shotgun surgery across three files, and a missed copy is a silent divergence the parity suite may or may not catch depending on coverage.
- **Recommendation:** Hoist the exemption predicate into one shared pure function (e.g. `outcome.py` or a small `runaway.py`: `is_runaway_exempt(failure_phase, outcome_kind) -> bool`) that all three repos call; the repos keep only their row-fetching. For the default, let `DaemonConfig`/`StateContext` derive from a single module constant rather than re-declaring `3`.

### I4. The `max_in_flight = 1` correctness dependency is documented but not enforced.

- **What:** `MergingState` notes the merge/rebase race is "operationally sidestepped by `max_in_flight = 1`" (`states/merging.py:35`), and the SQLite path is a single connection behind one `RLock`. Nothing prevents an operator from setting `max_in_flight = 5` in TOML (`config.py:316` accepts any int ≥ default; there is no upper guard tied to the merge-race or engine). The system would start, run concurrently, and expose the un-fixed race + serialize every DB op through one mutex.
- **Evidence:** `states/merging.py:35`; `config.py:316`; `sqlite_repository.py:129–135`.
- **Why it matters:** Operability + reliability. A latent correctness constraint enforced only by a code comment erodes the first time someone tunes for throughput.
- **Recommendation:** Either (a) make `MergingState` robust to concurrent base-advance (foreman#316 is already tracked) and then lift the constraint deliberately, or (b) until then, validate at startup: if `storage.engine == "sqlite"` and `max_in_flight > 1`, warn loudly; gate `max_in_flight > 1` behind an explicit "I accept the merge-race" flag. Make the implicit explicit.

## Minor findings (worth knowing)

### M1. `tick_once` busy-waits on a 5s drain budget with a 10ms sleep loop.
- **What:** `daemon.tick_once` drains the pool with `while in_flight > 0 and budget > 0: time.sleep(0.01)` (`daemon.py:154–157`). It's documented as test-determinism scaffolding, but it runs in production every tick. A transition that legitimately takes >5s (a role subprocess can take up to `role_timeout_seconds = 600`) means the budget always expires and `tick_once` returns with work still in flight — harmless given the design, but the 5s number is a magic constant doing double duty (test determinism vs. production cadence).
- **Recommendation:** Split the concern: production `run_forever` doesn't need the bounded busy-wait (the `tick_seconds` sleep already gives drain time). Gate the drain loop to test usage, or name the constant.

### M2. `reload` reloads logging only, contradicting the README's "no restart needed" claim.
- **What:** `cmd_daemon_reload` sends SIGHUP, whose handler only resets logging (`cli/daemon.py:68–85`, `155–164`); it explicitly "does NOT re-read config from disk." The README states you add projects via `foreman init` then `foreman daemon reload` "to register them with the running daemon (no restart needed)" (`README.md`). These disagree — a newly-added project will not be picked up by reload.
- **Recommendation:** Either implement config reload-on-SIGHUP, or fix the README to say a restart is required to register new projects. Doc/behavior drift on an operational command is a runbook hazard.

### M3. `next_state` defensive fall-throughs route unknown outcome kinds to NeedsHelp — good, but silent.
- **What:** Several states have a defensive final branch routing any unexpected `OutcomeKind` to NeedsHelp (`states/merging.py:181–186`, `states/implementing.py:30`). This is the right default, but it's silent — a role emitting an unexpected kind (schema drift) lands a human-action escalation with no distinct signal that the *cause* was an unhandled kind.
- **Recommendation:** Log/emit a distinct marker on the fall-through so "role emitted a kind this state doesn't handle" is distinguishable from a genuine NeedsHelp.

### M4. `QueueManager.dequeue` is O(n) per call with full heap re-push of skipped entries.
- **What:** Each `dequeue` pops every blocked candidate into `skipped` and re-pushes them in a `finally` (`queue_manager.py:70–98`). At low ticket volume this is fine; at scale the per-tick cost is O(queue_depth × heap-ops). Not a current problem — noting it so the constraint is on record.
- **Recommendation:** None for now; revisit only if ticket volume rises materially.

## What's working (non-risks)

- **Third-party containment (PyGithub).** The vendor type lives behind the `GitProvider` Protocol; `from github import …` appears only at the seam (`pygithub_git_provider.py`) and the composition root (`cli/__init__.py:189`). Domain code, states, and observers never touch a PyGithub object. The token-refresh seam (rebuild the `Github` client before the 1h token expires) is a genuinely subtle correctness detail handled deliberately (`pygithub_git_provider.py:9–45`). Exemplary anti-corruption layer.
- **Enforced boundaries.** `import-linter` contracts R1 (prod ⊄ tests) and R2 (`foreman.v4` ⊄ v3-substrate) are wired into `just check` and **both verified KEPT** this review. R2 in particular mechanically prevents v4 from regressing onto the dead v3 reconciler/plumbing during the coexistence period — exactly the right tool for a phased migration. The contract list is rule-sourced (each names the bug it prevents), not speculative.
- **Testability seams.** Every external dependency (`TicketRepository`, `GitProvider`, `RoleDispatcher`) is a `typing.Protocol` with an in-memory fake that the same contract suite holds to behavioral parity with the real impls (`repository.py:184`, `git_provider.py:199`, `role_dispatcher.py:38`). This is the backbone of the whole codebase's quality and it's done right.
- **Subprocess resource lifecycle.** `SubprocessRoleDispatcher._run_and_stream` guarantees kill+reap of the child and join of both reader threads on *every* exit path (success / timeout / exception / KeyboardInterrupt) via a single `try/finally` that includes thread construction (`subprocess_dispatcher.py:359–461`). Reader threads keep draining a broken pipe to avoid deadlocking the child on a full buffer (`subprocess_dispatcher.py:148–204`). There IS a subprocess timeout (`role_timeout_seconds`, default 600 — `config.py:321`), with a bounded post-kill wait — the classic "hung child stalls the worker forever" failure mode is genuinely handled.
- **Event-bus exception isolation.** Observer exceptions are caught, contextualized (ticket vs. daemon-level events), and logged without ever failing a transition (`event_bus.py:44–89`). The durability path is firewalled from observability side-effects.
- **Config as a validated boundary.** `V4Config` is a Pydantic model with `extra="forbid"`, required per-role app blocks, and a `model_validator` that refuses `postgres` without a DSN (`config.py:281–287`, `311–349`). TOML (untrusted input) fails fast at load with a clear error rather than half-starting.
- **Crash-idempotent merge path.** `attempt_merge` re-derives from `get_pr_state` and treats already-merged as CLEAN (`states/merge_helper.py:130–145`), so a crash between merge and journal-advance is self-healing for the merge states. This is the correct pattern — the finding (C1/I1) is that it isn't applied to the role-dispatch states.

## Failure modes

| Failure | Impact | Mitigation (present? recommended?) |
|---|---|---|
| Daemon crash mid-`execute()` | Orphan in-flight row counts toward runaway cap → false NeedsHelp; row leaks | **Absent.** Recommend C1 startup reconciliation. |
| Daemon crash after role side-effect, before journal advance | Possible duplicate PR/comment on re-run (role-dispatch states) | **Partial** — relies on deferred role idempotency (`poller.py:9–16`). Recommend I1 idempotency key. |
| Worker future raises before `transition()` records failure | Ticket silently re-enqueued forever; evidence only in logs | **Weak** — log-only (`worker_pool.py:111–120`). Recommend I2 state record. |
| Role subprocess hangs | Worker thread blocked | **Present** — `role_timeout_seconds` + kill/reap + bounded join (`subprocess_dispatcher.py:382–433`). |
| GitHub App token expiry at ~60 min | 401, daemon stops making progress | **Present** — client-rebuild seam cooperating with registry safety window (`pygithub_git_provider.py:9–45`). |
| Anthropic-side transient (5xx/429) | Role fails | **Present** — `TRANSIENT_PROVIDER_ERROR` → backoff schedule → NeedsHelp on exhaustion (`states/role_dispatch.py:51–146`). |
| Observer raises (e.g. GitHub label write fails) | Could fail a transition | **Present** — bus isolation (`event_bus.py:44–89`). |
| Perpetually-behind PR (base churn) | `MergingState` heal loop never converges | **Present** — `MAX_HEAL_ACTIONS = 5` bound → NeedsHelp (`states/merge_helper.py:52–93`, `151–170`). |
| `max_in_flight > 1` on SQLite | Merge/rebase race re-opens; all DB ops serialized through one mutex | **Absent at code level** — comment-only (`states/merging.py:35`). Recommend I4 guard. |
| Concurrent SQLite writes | Corruption / lock errors | **Present** — single conn + RLock + WAL (`sqlite_repository.py:101–135`); Postgres uses a real pool. |

## Current architecture (as-is)

```mermaid
flowchart TD
    subgraph entry [Entry / composition root]
        CLI["foreman.v4.cli:main (typer)"]
        BOOT["bootstrap_cli_context\n(the only wiring site)"]
        CFG["V4Config (pydantic, TOML boundary)"]
        CLI --> BOOT
        CFG --> BOOT
    end

    subgraph daemon [Daemon tick loop — single thread]
        DA["Daemon.run_forever / tick_once"]
        POLL["Poller (one per project)\nproducer"]
        QM["QueueManager\npriority heap + 3 filters\n(in-flight / held / deps)"]
        WP["WorkerPool\nThreadPoolExecutor(max_in_flight)"]
        BK["BackupScheduler (SQLite only)"]
        DA --> POLL --> QM
        DA --> WP
        WP -->|dequeue| QM
        DA --> BK
    end

    subgraph machine [State machine]
        TS["TicketState.transition()\nTemplate Method:\ncan_run→cap→enter→execute→verify→next→exit"]
        RDS["RoleDispatchState\n(Planning/SpecReview/SpecFix/\nImplementing/ImplReview/ImplFix)"]
        MS["MergingState / SpecMerging\n(re-derive from GitHub; crash-idempotent)"]
        TERM["Terminal: Done / Failed / NeedsHelp"]
        WP --> TS --> RDS
        TS --> MS
        TS --> TERM
    end

    subgraph seams [Protocol seams + fakes]
        REPO["TicketRepository\n(InMemory / SQLite / Postgres)"]
        GIT["GitProvider\n(Fake / PyGithub) via RoutingGitProvider"]
        DISP["RoleDispatcher\n(Fake / Subprocess)"]
    end

    subgraph obs [EventBus (sync) + observers]
        BUS["EventBus (exception-isolated)"]
        OBS["StructuredLog · EventArchive ·\nLabelObservability · Metrics ·\nSustainedBlocked · TerminalLanding"]
        BUS --> OBS
    end

    RDS -->|dispatch role| DISP
    DISP -->|subprocess: foreman plan/review/fix/implement| ROLES["v3 foreman.roles.*\n(Planner/Reviewer/Fixer/Worker)\nrun in per-ticket git worktree"]
    ROLES -->|FOREMAN_OUTCOME: json on stdout| DISP
    MS --> GIT
    POLL --> GIT
    TS -->|journal writes| REPO
    QM --> REPO
    TS -->|publish events| BUS
    OBS --> GIT
    OBS --> REPO

    R2["import-linter R2:\nv4 ⊄ v3-substrate (KEPT)"]
    ORPHAN["⚠ list_in_flight_state_instances()\ndefined but NEVER called →\nno startup reconciliation (C1)"]
    REPO -.->|gap| ORPHAN
```

## Decisions to record (ADR-ready)

### ADR: Crash-recovery reconciliation for orphaned in-flight state instances
- **Context:** The daemon is long-running and will restart (deploys, OOM, host reboot). A crash mid-`transition()` leaves a `state_instances` row open (`exited_at NULL`, no outcome). `list_in_flight_state_instances()` exists but is never called; orphans count toward `count_consecutive_same_state` (escalating tickets to NeedsHelp falsely) and leak. The merge states already recover by re-deriving from GitHub; the journal does not recover its own bookkeeping.
- **Decision:** On daemon startup, run a single reconciliation pass: for every in-flight `state_instances` row, record a synthetic `failure_phase = "crash_recovery"` failure and close the row; add `"crash_recovery"` to the runaway-exemption skip set so recovered crashes do not count toward any cap. Re-enqueue (already automatic via Poller) then proceeds cleanly.
- **Consequences:** Positive — restart becomes genuinely safe; journal stops leaking; false escalations stop. Negative — re-dispatch of role states still depends on role idempotency (see the companion idempotency decision); reconciliation must run before the first tick. Neutral — one new repository-free method on the daemon, using existing primitives.
- **Alternatives considered:** (a) Do nothing / rely on operators — rejected: the failure is silent and self-perpetuating. (b) Transactionally tie role side-effects to the journal advance — rejected: side-effects are external (GitHub), can't be in a DB transaction. (c) Mark orphans and let the next dispatch decide — viable as the *role-idempotency* half (I1), complementary, not a substitute for closing/exempting the row.

### ADR: Role-subprocess idempotency contract for safe re-dispatch
- **Context:** Role-dispatch states re-run the full subprocess on restart; a crash after a GitHub side-effect but before the journal advance can double-act. The dispatcher already exports `FOREMAN_STATE_INSTANCE_ID` for dedup, but role-side enforcement is deferred (spec C3).
- **Decision:** Make a stable per-state-instance idempotency key a required part of the role contract: a role re-invoked with a key it has already acted on must detect and no-op the side-effect (find-existing-PR-by-head-branch already exists on `GitProvider` and is the natural primitive).
- **Consequences:** Positive — crash-during-role becomes safe, closing the I1 gap. Negative — every role CLI must honor the contract; needs test coverage. Neutral — leverages an env var already plumbed.
- **Alternatives considered:** (a) Leave as deferred — rejected: silent duplicate-PR risk in an autonomous loop. (b) Two-phase commit across GitHub + DB — rejected as over-engineering for the volume.

## Next steps

Ordered, pragmatic. Do NOT implement any of these without the human's go-ahead — they're for selection.

1. **C1 — add startup reconciliation** (small, high value, uses existing primitives). The single highest-leverage fix.
2. **I2 — record worker-future crashes as ticket state** (small) so silent re-enqueue loops become visible.
3. **I4 — guard `max_in_flight > 1` on SQLite / merge-race** (small) — make the implicit `=1` dependency explicit until foreman#316 lands.
4. **I3 — hoist the runaway-exemption predicate into one shared function** (small refactor) before adding C1's new exempt phase, so it's a one-line change in one place.
5. **I1 — promote the deferred role-idempotency risk to tracked work** and implement the key contract (medium).
6. **M2 — reconcile `reload` behavior with the README** (trivial doc fix or a real config-reload feature — pick one).
7. Defer M1, M3, M4 unless throughput/volume rises.

If useful, I can also draft a short `docs/architecture/v4-overview.md` — the repo has `docs/architecture/` but no single current-state overview of the v4 daemon/state-machine/seam topology; this review's diagram + component map is a ready starting point.

---
*This review recommends; it does not modify code. Tell me which findings to act on and I'll take them one at a time.*
