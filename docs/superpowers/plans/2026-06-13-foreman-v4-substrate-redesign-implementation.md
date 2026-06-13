# Foreman v4 substrate redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace foreman's label-as-state coordination substrate with a SQLite-owned state machine, single-PR-per-ticket workflow, FastAPI webhook ingestion (exposed via tailscale funnel), and a typer CLI — preserving the existing role pipeline (Planner / Reviewer-on-spec / Fixer / Worker / Reviewer-on-impl / Fixer-on-impl) and the `needs-help` escalation pattern.

**Architecture:** State pattern with five-hook lifecycle (`can_run`/`enter`/`execute`/`verify`/`exit`) orchestrated by a Template Method base class. Mediator `QueueManager` decouples producers (WebhookReceiver, ReconciliationPoller) from the State Machine from the Worker Pool. Observer pattern routes side effects (SQLite persistence, GitHub label observability, structured logging) away from the state classes. Repository pattern over SQLite provides a testable persistence seam. Single PR per ticket (draft during spec phase → ready during impl phase → enqueued in GitHub MergeQueue for the actual merge), eliminating the O(N²) CI thrash from the v3 two-PR pattern.

**Tech Stack:** Python 3.12, uv workspace, SQLite + sqlalchemy core, FastAPI + uvicorn, pydantic, typer + rich, pytest + pytest-asyncio. Cross-platform Windows + Linux. GitHub MergeQueue for serialized merges. Tailscale funnel for public webhook URL.

**Branch:** All work lands on `feat/foreman-v4-substrate` off `main`. Single PR.

**Pre-push gate:** `just check` (ruff + mypy + import-linter + pytest) must stay green at every commit. Pre-push hook is host-native Windows; pytest runs against the host venv.

**Commit cadence:** Frequent. Each task ends with a commit. Conventional commits, lowercase subjects. Stage specific files (no `git add -A`).

**Source of truth:** `docs/superpowers/specs/2026-06-13-foreman-v4-substrate-redesign-design.md`. Cite section names when referencing it from a task.

---

## Phases

The plan is organized into phases that mirror the topological dependency order in the spec's "Approach" section. Each phase produces working, testable software on its own — useful as checkpoint boundaries for subagent-driven execution.

- **Phase 1 — Foundation.** Repository abstract + in-memory impl + SQLite impl + schema for `tickets` and `state_instances`. The `Outcome` model. The `TicketState` ABC + Template Method `transition()`. Tests are pure unit (no GitHub, no daemon). Completion = state machine works in isolation.
- **Phase 2 — Events + Observers.** `Event` base, `EventBus`, four concrete observer protocols + impls (`SQLitePersistenceObserver`, `LabelObservabilityObserver`, `StructuredLogObserver`, `MetricsObserver` no-op stub). Completion = side-effects fan out via the EventBus.
- **Phase 3 — Concrete states.** All 11 `TicketState` subclasses. Each state's `enter`/`execute`/`verify`/`exit` documented. Completion = end-to-end ticket lifecycle test passes against `FakeGitProvider`.
- **Phase 4 — QueueManager + Commands.** Mediator implementation + `Command` classes. Worker pool dispatch. Completion = lifecycle test now flows through the QueueManager.
- **Phase 5 — Webhook ingestion.** FastAPI `WebhookReceiver` with HMAC signature verification, payload normalization, dedup. Completion = mocked-webhook tests advance a ticket end-to-end.
- **Phase 6 — Reconciliation fallback.** `ReconciliationPoller` for downtime catch-up. Completion = poller emits delta events when webhook stream was offline.
- **Phase 7 — Role-side Outcome reporting.** Modify each role's `cli.py` entry point to emit `Outcome` JSON on stdout instead of writing labels. Role logic + prompts unchanged. Completion = roles produce stdout-parsable outcomes.
- **Phase 8 — Typer CLI.** Operator surface — `ps`, `show`, `log`, `queue`, `daemon`, `hold/resume/retry/skip/drop/set-state`, direct role invocations. Rich-formatted output. Completion = full operator command set usable against an in-memory repository in tests.
- **Phase 9 — Rich logging + MergeQueue default.** `RichHandler` + `JsonLinesHandler` configured at daemon startup. `DaemonConfig.merge_mechanism` defaults to `queue`. Completion = colored stdout + JSON file + queue is the merge default.
- **Phase 10 — v3 deletion + cutover docs.** Remove `reconciler/rules.py` + label-mutating action handlers + the `reconciler.py` crash-recovery module. Add per-repo webhook setup checklist to `docs/RUNBOOK.md`. Completion = grep for `_LABEL_TO_ACTION` returns zero; `just check` green; RUNBOOK explains tailscale-funnel + GitHub webhook config.

Phases 1–4 build the substrate in isolation (tests use in-memory fakes). Phases 5–7 wire real GitHub. Phases 8–10 round out the operator + cutover story.

---
