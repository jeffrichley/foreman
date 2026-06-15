# Foreman v4 substrate redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace foreman's label-as-state coordination substrate with a SQLite-owned state machine, two-phase PR workflow (spec PR + impl PR, MergeQueue on impl only), a single polling loop reading SQLite + GitHub, and a typer CLI — preserving the existing role pipeline (Planner / Reviewer-on-spec / Fixer / Worker / Reviewer-on-impl / Fixer-on-impl) and the `needs-help` escalation pattern.

**Architecture:** State pattern with five-hook lifecycle (`can_run`/`enter`/`execute`/`verify`/`exit`) orchestrated by a Template Method base class. Mediator `QueueManager` decouples the Poller from the State Machine from the Worker Pool. Observer pattern routes side effects (SQLite persistence, GitHub label observability, structured logging) away from the state classes. Repository pattern over SQLite provides a testable persistence seam. Two-phase PR preserved (spec PR + impl PR); MergeQueue enabled on the impl PR only.

**Tech Stack:** Python 3.12, uv workspace, SQLite (stdlib `sqlite3`), pydantic v2, typer + rich, pytest + pytest-asyncio. Cross-platform Windows + Linux. GitHub MergeQueue for serialized merges on impl PRs.

**Branch:** All work lands on `feat/foreman-v4-substrate` off `main`. Single PR.

**Pre-push gate:** `just check` (ruff + mypy + pytest) must stay green at every commit. Pre-push hook is host-native Windows; pytest runs against the host venv.

**Commit cadence:** Frequent. Each task ends with a commit. Conventional commits, lowercase subjects. Stage specific files (no `git add -A`).

**Source of truth:** `docs/superpowers/specs/2026-06-13-foreman-v4-substrate-redesign-design.md`. Cite section names when referencing it from a task.

**Execution cadence:** One phase, then stop for human review before the next phase begins. Phase files live as siblings under `v4-phases/` (see "Phases" section at bottom).

---

## v4 isolation principle — "delete v2/v3 by `rm -rf`"

Every task in this plan is written so that Phase 8 (cutover) can delete v2 + v3 with directory-level operations alone. No grep-and-patch. No untangling. The discipline that makes this true:

**1. Namespace.** All v4 code lives under `packages/foreman/src/foreman/v4/`. All v4 tests live under `packages/foreman/tests/v4/`. The `foreman.v4.*` import path is the v4 boundary forever — there is no rename at cutover. (Same shape protects future v5 from the same churn.)

**2. v4 never imports legacy modules.** A v4 module MAY import from:

  - the Python standard library
  - third-party deps already in `pyproject.toml` (pydantic, typer, rich, pygithub, sqlalchemy if added, etc.)
  - other `foreman.v4.*` modules
  - the **survival set** named below — modules that pre-date v4 but are not v2/v3-specific (auth, config, identity, worktree, etc.)

  A v4 module MUST NOT import from the **kill set** named below. Any task whose code or test reaches into the kill set is a bug in the task — fix the task, not the import.

**3. Survival set.** These files pre-date v4, are not coupled to the v2/v3 state-machine substrate, and v4 calls into them:

  - `foreman/auth.py` — GitHub PAT + App-token loading
  - `foreman/config.py` — TOML loading + env override (v4 adds keys, doesn't replace the loader)
  - `foreman/identity.py` — per-role PyGithub clients
  - `foreman/init.py` — `foreman init` project bootstrap (CLI command moves to typer wrapper in Phase 6, but the function survives)
  - `foreman/instructions.py` — CLAUDE.md fragment writer
  - `foreman/locks.py` — generic file-locking primitive
  - `foreman/git_host.py` + `foreman/git_hosts/` — Git provider abstraction
  - `foreman/provider.py` + `foreman/providers/` — LLM provider abstraction
  - `foreman/roles/{planner,reviewer,fixer,worker}.py` — role logic (Phase 5 modifies only the CLI exit path to emit `FOREMAN_OUTCOME:` JSON; the role bodies stay)
  - `foreman/prompts/` — role prompt files (unchanged)
  - `foreman/worktree.py` — per-ticket git worktree
  - `foreman/_env_filter.py` — env scrubbing
  - `foreman/logging_setup.py` — Phase 7 extends; doesn't replace

**4. Kill set — Phase 8 `git rm`-able with no v4 fallout.** These are pure v2/v3 substrate; nothing in `foreman.v4.*` reaches them:

  - `foreman/reconciler/` — entire directory (rules engine, v3 daemon, v3 host adapter, label-mutating actions)
  - `foreman/daemon.py` — v3 daemon main loop
  - `foreman/daemon_runners.py` — v3 label-triggered role dispatch entrypoints (replaced by `foreman.v4.dispatch`)
  - `foreman/daemon_host.py` — v3 daemon GitHub adapter (replaced by `foreman.v4.poller` direct PyGithub use)
  - `foreman/daemon_lock.py` — v3 lock file (replaced by `foreman.v4.daemon_lock` if needed)
  - `foreman/dispatcher.py` — the `_LABEL_TO_ACTION` map; v4 has no analog
  - `foreman/dispatch_recorder.py` — v3 dispatch journal (replaced by `state_instances` table)
  - `foreman/poller.py` — v3 poller (replaced by `foreman.v4.poller`)
  - `foreman/queue.py` — v3 queue (replaced by `foreman.v4.queue_manager`)
  - `foreman/storage.py` — v3 SQLite schema (replaced by `foreman.v4.sqlite_repository` + `schema.sql`)
  - `foreman/worker.py` — v3 worker loop (replaced by `foreman.v4.worker_pool`)
  - `foreman/role_dispatch.py` — v3 role dispatch helper
  - `foreman/stats.py` — v3 stats (the v4 CLI computes from `state_instances` directly)
  - `foreman/ps.py` — v3 `ps` command (replaced by `foreman.v4.cli.ps`)
  - `foreman/labels.py` — v3 label catalog. v4's `LabelObservabilityObserver` ships its own minimal write-only label vocabulary; nothing reads from `labels.py`. **DELETE in Phase 8.**
  - `foreman/branches.py` — v3 branch resolution (replaced by `foreman.v4.branches` if any survives)
  - `foreman/v3_bus_endpoint.py` — v3 bus integration
  - `foreman/cli.py` — top-level CLI dispatcher. Phase 6 moves the v4 commands into `foreman/v4/cli/`; Phase 8 deletes the v2/v3 commands and rewrites this file to a thin wrapper that exposes only the typer app from `foreman.v4.cli`.

**5. Tests.** v4 tests live under `tests/v4/` and never import fixtures from `tests/reconciler/`, `tests/daemon/`, `tests/dispatcher/`, etc. Phase 8 deletes the legacy test directories alongside the legacy code.

**6. v4 SQLite is a new file.** v4 connects to a different DB path (`<project>/.foreman/v4/state.db`) than v3 used. Cutover does not require schema migration — v3 DB is abandoned in place. Phase 8 documents the path change in `docs/RUNBOOK.md`.

**Phase 8 cutover, in one shot:**

```bash
# Remove the kill set
git rm -r packages/foreman/src/foreman/reconciler/
git rm packages/foreman/src/foreman/daemon.py
git rm packages/foreman/src/foreman/daemon_runners.py
git rm packages/foreman/src/foreman/daemon_host.py
git rm packages/foreman/src/foreman/daemon_lock.py
git rm packages/foreman/src/foreman/dispatcher.py
git rm packages/foreman/src/foreman/dispatch_recorder.py
git rm packages/foreman/src/foreman/poller.py
git rm packages/foreman/src/foreman/queue.py
git rm packages/foreman/src/foreman/storage.py
git rm packages/foreman/src/foreman/worker.py
git rm packages/foreman/src/foreman/role_dispatch.py
git rm packages/foreman/src/foreman/stats.py
git rm packages/foreman/src/foreman/ps.py
git rm packages/foreman/src/foreman/labels.py
git rm packages/foreman/src/foreman/branches.py
git rm packages/foreman/src/foreman/v3_bus_endpoint.py
git rm -r packages/foreman/tests/reconciler packages/foreman/tests/daemon packages/foreman/tests/dispatcher
# Rewrite foreman/cli.py to wrap foreman.v4.cli (~10 lines)
# Run just check; expect green.
```

If `just check` is green after these `git rm`s, the isolation discipline held. If it's red, the failing import is the receipt for which task or module violated the principle — fix the source, not the symptom.

---

## Phases

Each phase lives in its own file under [`v4-phases/`](./v4-phases/). Execute one phase, run `just check`, then stop for human review before opening the next file. Per-phase task counts in parens.

| # | Phase | File | Completion criterion |
|---|---|---|---|
| 1 | **Foundation** — `Outcome` model, `FOREMAN_OUTCOME:` parser, SQLite schema, records, `TicketRepository` + in-memory + SQLite impls (shared contract suite), `TicketState` ABC + Template Method `transition()`, isolation-guard test (10 tasks) | [v4-phases/phase-1-foundation.md](./v4-phases/2026-06-13-foreman-v4-phase-1-foundation.md) | State machine works in isolation. |
| 2 | **Events + Observers** — five lifecycle events, `EventBus` with subscriber exception isolation, `StructuredLog` / `LabelObservability` / `EventArchive` / `Metrics` observers, fan-out integration (8 tasks) | [v4-phases/phase-2-events-observers.md](./v4-phases/2026-06-13-foreman-v4-phase-2-events-observers.md) | Side effects fan out via the EventBus. |
| 3 | **Concrete states** — `RoleDispatcher` + fake, `GitProvider` + fake, 11 `TicketState` subclasses, state registry, end-to-end lifecycle test (10 tasks) | [v4-phases/phase-3-concrete-states.md](./v4-phases/2026-06-13-foreman-v4-phase-3-concrete-states.md) | Lifecycle test passes against `FakeGitProvider`. |
| 4 | **QueueManager + Poller (concurrent + priority + deps)** — Repository helpers (PR lookup + dep filter + count + WAL), `WorkItem`, **priority-queue** `QueueManager` (per-ticket FIFO + held + unmet-deps multi-filter), **`ThreadPoolExecutor`** `WorkerPool`, `Poller`, `PyGithubGitProvider` (real impl), e2e with 3 concurrent tickets + dep-blocked downstream (7 tasks) | [v4-phases/phase-4-queuemanager-poller.md](./v4-phases/2026-06-13-foreman-v4-phase-4-queuemanager-poller.md) | 3 concurrent tickets drive to Done; dep-blocked one waits for upstream. |
| 5 | **Role-side Outcome reporting** — `emit_outcome` helper, Planner / Reviewer / Fixer / Worker CLI rewrites (label-writing tails deleted), `SubprocessRoleDispatcher` (real impl), subprocess fork e2e (7 tasks) | [v4-phases/phase-5-role-outcome.md](./v4-phases/2026-06-13-foreman-v4-phase-5-role-outcome.md) | Roles produce stdout-parsable outcomes. |
| 6 | **Typer CLI** — `CliContext` dataclass + `build_cli_context()` (single builder), output formatter Strategy, `ps`/`show`/`log`/`queue`, mutations (`hold/resume/retry/skip/drop/set-state`), `daemon start/stop/reload/status`, role commands migrated to typer, operator e2e (7 tasks) | [v4-phases/phase-6-typer-cli.md](./v4-phases/2026-06-13-foreman-v4-phase-6-typer-cli.md) | Full operator surface usable in tests. |
| 7 | **Rich logging + MergeQueue default + bootstrap** — `JsonLinesHandler`, `configure_logging`, `V4Config` (pydantic, TOML), multi-project `Daemon` refactor, `bootstrap_cli_context`, config-to-Done e2e (6 tasks) | [v4-phases/phase-7-logging-bootstrap.md](./v4-phases/2026-06-13-foreman-v4-phase-7-logging-bootstrap.md) | Colored stdout + JSON file + queue is the merge default. |
| 8 | **Finalize coding + wiring (dogfood-ready)** — `bootstrap_cli_context` owns `EventBus` + observers, public `Daemon.shutdown()`, `V4Config` `[apps]` + `[orchestrator]` sections, real `IdentityRegistry` in `main()` (drops the 2 `# type: ignore`), `reset_logging()` on daemon reload, real-fork integration test against installed `foreman` binary, manual dogfood (one real ticket end-to-end) (7 tasks) | [v4-phases/phase-8-finalize-coding-wiring.md](./v4-phases/2026-06-13-foreman-v4-phase-8-finalize-coding-wiring.md) | `just check` green + Task 8.7 reports a real ticket transitioning to a terminal state on a real test repo. |
| 8b | **Role CLI v4 config rewire + `foreman init` + Windows daemon-status fix** — surfaced by Task 8.7 dogfood: role CLIs hard-imported v3 config (KeyError on new project); v4 CLI exposed no `init` to create labels/verify bots/validate clone; `cmd_daemon_status`/`stop` raised OSError on Windows. v4 `ProjectConfig` grows 4 fields; role CLIs read v4 config + build v3 Config-shaped object for legacy `run_<role>` calls; new `foreman init` typer command delegates to `foreman.init.run_init`; OSError catch on daemon status/stop (5 tasks, last one re-runs the dogfood) | [v4-phases/phase-8b-role-cli-rewire.md](./v4-phases/2026-06-15-foreman-v4-phase-8b-role-cli-rewire.md) | Dogfood re-run reaches Done (or NeedsHelp on real role decision) — NOT a config-shaped crash. |
| 9 | **v3 deletion + cutover docs + PR** — execute `git rm` block, repair survival-set orphaned imports, RUNBOOK additions (MergeQueue setup + daemon config + cutover procedure), adversarial review pass, push branch, open PR (5 tasks) | [v4-phases/phase-9-deletion-cutover.md](./v4-phases/2026-06-13-foreman-v4-phase-9-deletion-cutover.md) | `_LABEL_TO_ACTION` returns zero matches; `just check` green; RUNBOOK explains MergeQueue per-repo enablement. |

Phases 1–4 build the substrate in isolation (tests use in-memory fakes). Phases 5–6 wire real GitHub + the operator surface. Phase 7 rounds out logging + bootstrap. Phase 8 finalizes daemon-side coding + wiring; Phase 8b lands the role-CLI + init + Windows fixes that Phase 8.7 dogfood surfaced. Phase 9 is the (now truly mechanical) v3 cutover.

---

## Plan complete

Nine phases, ~67 tasks, ~13k lines of plan content. Each task is bite-sized + TDD-shaped + commit-bounded. The substrate replacement runs from foundation → observers → states → orchestration → role rewrite → CLI → logging+bootstrap → finalize coding+wiring (dogfood-ready) → cutover.

After this plan executes:
- `foreman.v4.*` is the only coordination substrate in the repo
- Operator surface is `foreman ps` / `foreman show` / `foreman log --tail` / `foreman daemon start` / mutations
- New tickets flow through Queued → Planning → SpecReview → Implementing → ImplReview → Merging → Done with the same Planner/Reviewer/Fixer/Worker prompts the v3 daemon used
- `_LABEL_TO_ACTION` does not exist
- v4 SQLite is the single source of truth for "what phase is each ticket in"
- Labels on GitHub issues are observability output only — never read by the daemon

**Execution model:** subagent-driven development (per the spec's recommendation), task-by-task, with continuous execution **within a phase**. Each phase ends at its `just check` gate; the SDD loop stops there and waits for human review before opening the next phase file.
