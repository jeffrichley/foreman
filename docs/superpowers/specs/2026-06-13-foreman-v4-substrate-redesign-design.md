# Spec: Foreman v4 — substrate redesign (state machine + polling + CLI)

## Goal

Replace foreman's current coordination substrate — GitHub labels as state machine, label-driven rules engine — with a daemon-owned state machine in SQLite, a single polling loop, and an operator-facing CLI. Preserve the existing role pipeline (Planner → Reviewer-on-spec → Fixer → Worker → Reviewer-on-impl → Fixer-on-impl), the **two-phase PR pattern** (spec PR then impl PR), and the `needs-help` escalation pattern.

**Motivation.** The current substrate is brittle. Labels-as-state has produced the recurrent failure class documented across issues #160, #170, #303, #307 (stale labels silently disabling rule predicates, two-writer races, no atomicity). The two-PR-per-ticket pattern produces O(N²) CI thrash when multiple tickets are queued — every merge to `main` invalidates every other in-flight PR's base. The brittleness is *operational*, not architectural: the roles do good work, but the coordination fabric breaks. v4 keeps the roles, rewrites the fabric.

**Scope of this decision.** Establish the v4 substrate architecture. Subsequent implementation plan (via writing-plans) decomposes into bite-sized tasks against this spec.

## Acceptance criteria

- **State machine owns workflow state.** Daemon SQLite is the single source of truth for "what phase is each ticket in." GitHub labels are output-only — written by the daemon as observable status, never read back by the daemon.
- **Five-hook lifecycle per state.** Each `TicketState` subclass exposes `can_run`, `enter`, `execute`, `verify`, `exit` — each with a single responsibility, each independently testable, each with a distinct failure handler.
- **Two-phase PR (preserved from v3).** Planner opens a spec PR against `main` carrying the spec doc commit. Reviewer-on-spec sees only the spec commit in the diff. Once approved + merged, Worker opens an impl PR against `main`. Reviewer-on-impl sees only the implementation diff. **MergeQueue is enabled on the impl PR only** — the spec PR is small and merges fast without queue mechanics. Rejected the single-PR proposal during the 2026-06-13 adversarial review (it conflated Reviewer-on-spec's and Reviewer-on-impl's task surfaces and reintroduced intra-PR thrash from Fixer-on-spec amendments under in-flight Worker commits).
- **MergeQueue eliminates N² CI thrash.** Default `MergeMechanism = queue` for autonomous-loop PRs. GitHub serializes the rebase + CI dance; foreman just enqueues.
- **Typer CLI surface** covering query (`ps`, `show`, `log`, `queue`, `daemon status`), mutation (`hold`, `resume`, `retry`, `skip`, `drop`, `set-state`), daemon lifecycle (`start`, `stop`, `reload`), and direct role invocation (`plan`, `review`, `fix`, `implement`).
- **Rich-formatted output by default.** `ps` renders as `rich.Table`; `show` renders state history as `rich.Tree` with outcome-colored branches; `log --tail` uses `rich.Live`; daemon stdout uses `RichHandler` for human-readable transitions, with the JSON-lines file handler retained for machine-readable persistence.
- **Observer pattern routes side effects.** State hooks emit Events; `SQLitePersistenceObserver`, `LabelObservabilityObserver`, `StructuredLogObserver` subscribe. Adding a metrics emitter, Slack notifier, etc. = new observer class, no state-machine changes.
- **No `rules.py` predicate engine.** The label-pattern engine (`packages/foreman/src/foreman/reconciler/rules.py`) is deleted. Replaced by `state.execute() → outcome → state.next_state(outcome)` — each state owns its own next-state decision; there's no central rules table.
- **Crash-only resume.** Every state transition committed to SQLite *before* the next role dispatches. Daemon restart reads last state from SQLite and resumes.
- **Existing role prompts unchanged.** Planner, Reviewer (both targets), Fixer (both targets), Worker prompts behave identically. Roles report structured Outcome via subprocess stdout instead of writing labels.

## Approach

The redesign uses standard SOLID principles and three GoF patterns explicitly:

- **State pattern** for the workflow. Each phase is a concrete `TicketState` subclass with its own enter/execute/exit semantics. The state machine holds a current state and transitions via state-returned next-state.
- **Template Method pattern** on the abstract `TicketState`. The base class defines a `transition()` Template Method that orchestrates the five-hook lifecycle in fixed order with per-phase failure handlers; subclasses override individual hooks.
- **Mediator pattern** for the `QueueManager`. The Poller, the State Machine, and the Worker Pool all talk through the QueueManager, never directly. Decouples the producer-consumer surface. QueueManager carries per-state dispatch policy as configurable rules — e.g. "while in `ImplCIWait`, the worker-pool slot is freed for other tickets" (the scheduler layer that sits on top of the discrete state machine; addresses the "let the workflow flow" point from the 2026-06-13 adversarial review).
- **Single polling loop, SQLite is the source of truth.** No webhooks, no FastAPI, no tailscale funnel dependency, no HMAC, no inbound network surface. The `Poller`:
  - Polls GitHub at one configurable cadence for two purposes — (a) detect new tickets via the trigger label, (b) read the artifact state of in-flight tickets (PR mergeable, CI verdict, MergeQueue rejection) by querying SQLite `state_instances WHERE exited_at IS NULL` to know what to ask GitHub about.
  - Normalizes GitHub responses into domain Events (`NewTicketEvent`, `CIVerdictEvent`, `PRMergedEvent`, `MergeQueueRejectedEvent`, etc.) and feeds the QueueManager.
  - Dedups by the SQLite ticket+state pair — "have we already advanced this state-instance based on the CI verdict for PR #X?" — so a repeated poll-read of the same GitHub artifact state doesn't double-process.
  - Webhook ingestion was considered (FastAPI + tailscale funnel) and rejected per the adversarial review's C4 (funnel URL stability is unsettled, primary-vs-fallback architecture rests on an open question, polling is what v3 does and works). Webhooks can be re-added later as an additive producer with the same Event interface; not in v4.
- **Observer pattern** for side effects. State hooks emit Events; concrete observers (SQLite persistence, GitHub label writes, structured logging) subscribe. SRP per observer; new observability surfaces are additive.
- **Repository pattern** for SQLite. `TicketRepository` is the only seam between domain code and SQLite. Test doubles use an in-memory implementation.
- **Strategy pattern** for CLI output formatting. `TableFormatter`, `JsonFormatter`, `YamlFormatter` implement a common interface; `--format=X` flag selects.

Command pattern was considered for queue work and rejected per the 2026-06-13 adversarial review M2 — polling reconstructs intended work from `(in-flight state-instances, GitHub artifact state)` on every tick, so there's no need for an in-flight queue of serialized Command objects. The QueueManager dispatches role subprocesses directly; that's sufficient for v4.

**Topological order of the rewrite:**

1. **`TicketRepository` + SQLite schema** for v4 (`states`, `transitions`, `events` tables).
2. **`TicketState` abstract + Template Method `transition()`**.
3. **Concrete states**: `QueuedState`, `PlanningState`, `SpecReviewState`, `SpecFixState`, `ImplementingState`, `ImplReviewState`, `ImplFixState`, `MergingState`, `DoneState`, `FailedState`, `NeedsHelpState`.
4. **Observer infrastructure** + `SQLitePersistenceObserver`, `LabelObservabilityObserver`, `StructuredLogObserver`.
5. **`QueueManager` (Mediator)** + Pollers + Worker Pool.
6. **Role-side `Outcome` reporting** — roles emit structured JSON on stdout instead of writing labels. Role prompt files unchanged.
7. **Typer CLI** — `ps`, `show`, `log`, `hold/resume/retry/skip/drop/set-state`, `daemon start/stop/reload`, direct role invocations.
8. **MergeQueue wiring** — set default `MergeMechanism = queue` for autonomous-loop PRs; verify branch-protection on target repos.
9. **`rules.py` deletion** — once states own their transitions, the predicate engine has no callers.
10. **Migration** — existing in-flight tickets transition to v4 by reading their current GitHub label state once, writing into the new SQLite tables, and resuming under v4 rules. Document one-shot migration script in the impl PR.

## Sub-requests

1. **Create `packages/foreman/src/foreman/state_machine/` package** with `TicketState` abstract, the Template Method `transition()`, the Outcome model, and per-failure handler defaults.
2. **Create the SQLite v4 schema** in `packages/foreman/src/foreman/storage_v4.py` — tables for `tickets`, `states`, `transitions`, `events`, `outcomes`. Migration from v3 schema documented.
3. **Implement `TicketRepository`** abstract + SQLite impl + in-memory test impl. The only seam to persistence.
4. **Implement concrete states** one per file under `state_machine/states/`. Each state's pre/post conditions documented in its `can_run`/`verify` hook docstring.
5. **Implement Observer infrastructure** — `Event` model, `Observer` protocol, `EventBus` singleton, three concrete observers.
6. **Implement `QueueManager`** (Mediator) — owns the work queue, dispatch order, concurrency caps. Talks to Pollers + WorkerPool via Commands.
7. **Implement role-side Outcome reporting** — modify each role's `cli.py` entry point to emit `Outcome` JSON on stdout instead of writing labels. Role logic unchanged.
8. **Build typer CLI** under `packages/foreman/src/foreman/cli_v4/` with one file per command. Top-level `cli.py` is a thin dispatcher.
9. **Wire rich logging** — `RichHandler` for stdout, `JsonLinesHandler` for file. Daemon startup configures both.
10. **MergeQueue default** — set `MergeMechanism = queue` as the default in `DaemonConfig`. Verify the existing queue path handles draft → ready transitions.
11. **Delete `rules.py`** and `reconciler/actions.py`'s label-mutating action handlers. Replace `_LABEL_TO_ACTION` map with state-machine dispatch.
12. **Implement the `Poller`** — single polling loop that reads SQLite (`state_instances WHERE exited_at IS NULL` + open tickets for trigger label) and queries GitHub for what's in flight: trigger labels on open issues, PR mergeable / CI / MergeQueue state for in-flight tickets. Normalizes responses into domain Events (`NewTicketEvent`, `CIVerdictEvent`, `PRMergedEvent`, `MergeQueueRejectedEvent`, etc.) and feeds the QueueManager. Dedup at the (ticket, state-instance, artifact-state) tuple — re-polling the same artifact state does not double-process. One configurable cadence; no FastAPI, no HMAC, no tailscale dependency.
13. **Update CLAUDE.md + per-project instruction files** with the v4 model. Role prompts unchanged.
14. **Quality gate**: `just check` green. New tests under `tests/state_machine/`, `tests/cli_v4/`, `tests/observers/`. Existing tests update for the v4 substrate or move to `tests/legacy/` if irrelevant.

## File-level changes

| File | Change |
| --- | --- |
| `packages/foreman/src/foreman/state_machine/__init__.py` | NEW. State machine package marker. |
| `packages/foreman/src/foreman/state_machine/base.py` | NEW. `TicketState` ABC, Template Method `transition()`, default failure handlers, `Outcome` model. |
| `packages/foreman/src/foreman/state_machine/states/{queued,planning,spec_review,spec_fix,implementing,impl_review,impl_fix,merging,done,failed,needs_help}.py` | NEW. One concrete state per file. |
| `packages/foreman/src/foreman/storage_v4.py` | NEW. SQLite v4 schema — `tickets` (with `held_by`/`held_at`/`held_reason` columns), `state_instances` (the journal table; see Durability section), `events`. No migration from v3 — clean break per the Migration path section. |
| `packages/foreman/src/foreman/repository.py` | NEW. `TicketRepository` abstract + SQLite impl + in-memory impl. |
| `packages/foreman/src/foreman/events.py` | NEW. `Event` base + concrete event classes + `EventBus`. |
| `packages/foreman/src/foreman/observers/{sqlite,label,log,metrics}.py` | NEW. Concrete observers. |
| `packages/foreman/src/foreman/queue_manager.py` | NEW. Mediator implementation. |
| `packages/foreman/src/foreman/v4/poller.py` | NEW. Single polling loop — reads SQLite (in-flight state instances + open tickets), queries GitHub, normalizes to domain Events, dedups by (ticket, state-instance, artifact-state). No HTTP server, no webhooks. |
| `packages/foreman/src/foreman/cli_v4/{ps,show,log,queue,daemon,hold,resume,retry,skip,drop,set_state,plan,review,fix,implement}.py` | NEW. One typer command per file. |
| `packages/foreman/src/foreman/cli_v4/__init__.py` | NEW. Top-level typer app dispatcher. |
| `packages/foreman/src/foreman/logging_setup.py` | UPDATE. Add `RichHandler` alongside `JsonLinesHandler`. |
| `packages/foreman/src/foreman/reconciler/rules.py` | DELETE. Replaced by state machine. |
| `packages/foreman/src/foreman/reconciler/actions.py` | DELETE entirely. The PR-merge and observation-only handlers move to the state-machine layer: PR-merge is owned by `MergingState.execute()`, observation-only reads are inlined into the Poller. Half-deletion was rejected by the 2026-06-13 adversarial review (I5) as architecturally inconsistent. |
| `packages/foreman/src/foreman/daemon_host.py` | UPDATE. Read methods retained (`get_issue_labels`, etc.); label-write methods routed through `LabelObservabilityObserver` only. |
| `packages/foreman/src/foreman/roles/{planner,reviewer,fixer,worker}.py` | UPDATE. Each role's exit emits `Outcome` JSON on stdout instead of writing labels. Role prompt + logic unchanged. |
| `packages/foreman/src/foreman/config.py` | UPDATE. Add `merge_mechanism` default = `"queue"` for autonomous-loop PRs. |
| `packages/foreman/tests/state_machine/` | NEW. Per-state tests + Template Method orchestration tests. |
| `packages/foreman/tests/observers/` | NEW. Observer unit tests. |
| `packages/foreman/tests/cli_v4/` | NEW. Typer CLI tests via `CliRunner`. |
| `packages/foreman/tests/repository/` | NEW. `TicketRepository` tests (in-memory + SQLite). |
| `packages/foreman/tests/lifecycle/` | NEW. End-to-end ticket-lifecycle test using `FakeGitProvider` (port the test from foreman#307 LabelManager branch). |

## Durability + resume

Every state's lifecycle leaves a permanent trail in `state_instances`. This table IS the journal — the same data backs the audit log (`foreman show <ticket>`), the crash-recovery procedure, and the operator pause/resume mechanic.

### `state_instances` schema

```sql
CREATE TABLE state_instances (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL,
    state_name TEXT NOT NULL,             -- e.g. "PlanningState"
    sequence INTEGER NOT NULL,            -- 1st, 2nd, ... time this ticket was in this state
    entered_at TIMESTAMP NOT NULL,        -- enter() returned successfully
    execute_started_at TIMESTAMP,         -- execute() began
    execute_completed_at TIMESTAMP,       -- execute() returned (success or failure)
    exited_at TIMESTAMP,                  -- exit() returned (always runs)
    outcome_kind TEXT,                    -- "clean" | "needs_fix" | "error" | "timeout" | NULL if in progress
    outcome_payload JSON,                 -- the structured outcome from execute()
    next_state TEXT,                      -- the next state we transitioned to (NULL if in progress)
    failure_phase TEXT,                   -- "can_run" | "enter" | "execute" | "verify" | "exit" | NULL
    failure_reason TEXT,
    UNIQUE(ticket_id, sequence)
);
```

Each timestamp marks one of the five lifecycle hooks completing. A row with `exited_at IS NULL` is an in-flight transition.

### Crash recovery is a query, not a separate component

On daemon restart:

```sql
SELECT * FROM state_instances WHERE exited_at IS NULL ORDER BY ticket_id, sequence;
```

For each in-flight row, the resume logic dispatches on which timestamps are set:

1. **Mid-execute crash** (row has `execute_started_at` but no `execute_completed_at`): the role subprocess died with the daemon. Daemon checks if the subprocess is still alive (PID check); if not, **re-dispatches** the role. Roles are designed idempotent — Planner checks "is there already a PR for this issue?" before opening one; Reviewer is purely read+report; Worker checks worktree state before mutating.
2. **Between-state crash** (row has all timestamps including `next_state`, but no new row for that next state): the daemon committed the outcome but crashed before entering the next state. Resume creates the new `state_instance` row and calls `enter()`.
3. **During-exit crash** (`exited_at IS NULL` but `execute_completed_at` is set): re-run `exit()`. Required to be idempotent — release resources, log; both naturally idempotent.

This **retires the v3 reconciler entirely.** v3 had a separate `reconciler.py` for crash recovery that diffed labels against expected state. v4 doesn't need it — SQLite IS the source of truth, the query above IS the recovery procedure.

### Operator pause / resume

Operator pause is layered on top of the state machine, NOT a state change. Three columns on the `tickets` row:

```sql
ALTER TABLE tickets ADD COLUMN held_by TEXT;
ALTER TABLE tickets ADD COLUMN held_at TIMESTAMP;
ALTER TABLE tickets ADD COLUMN held_reason TEXT;
```

- `foreman hold 307` sets `held_by='jeffrichley', held_at=NOW(), held_reason="manual"`. State machine refuses to dispatch new work while `held_by IS NOT NULL`.
- **Any in-flight `execute()` is allowed to complete** — pause takes effect at the next state boundary, not mid-LLM-call. This is intentional; aborting mid-LLM wastes the work and leaves artifact state inconsistent.
- `foreman resume 307` clears the hold; next poll picks the ticket up from its current state.

The same pattern serves the daemon's own shutdown: graceful stop = "drain in-flight execute() calls, don't dispatch new ones, exit when SQLite shows no in-flight rows." Hard stop = exit immediately; resume on restart via the crash-recovery query.

### Auditability

`foreman show <ticket>` walks `state_instances` for the ticket and renders the full state history with timing per state, outcome per execute, and failure reason per failure. Every state transition is a row; nothing is lost.

## Outcome JSON — role-side reporting contract

Every role (Planner, Reviewer-on-spec, Fixer-on-spec, Worker, Reviewer-on-impl, Fixer-on-impl) reports the result of one invocation to the daemon via a single line of structured JSON on stdout. This contract replaces the v3 label-writing mechanism that the roles currently use to communicate "I'm done; here's what happened" to the daemon.

The contract is small and explicit. The state machine reads stdout, parses the trailing JSON line, validates against a pydantic `Outcome` model, decides the next state, and writes the parsed outcome to `state_instances.outcome_payload`. If parsing fails, the state transitions to `Failed` with `failure_phase="verify"` (the daemon's verify hook owns parsing) and `failure_reason` carrying the raw stdout tail.

### Schema

```python
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field

class OutcomeKind(str, Enum):
    CLEAN = "clean"              # work completed; advance
    NEEDS_FIX = "needs_fix"      # reviewer found issues; route to fixer
    BLOCKED = "blocked"          # external dependency (CI, MergeQueue) — re-poll later
    NEEDS_HELP = "needs_help"    # escalate to human
    ERROR = "error"              # role itself failed (subprocess crash, internal exception)

class OutcomeConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

class Finding(BaseModel):
    """One reviewer-flagged issue. Only present when kind == NEEDS_FIX."""
    severity: Literal["critical", "important", "minor"]
    location: str = Field(..., description="file:line or 'general'")
    description: str

class OutcomeArtifacts(BaseModel):
    """URLs / IDs the next state may need. All optional."""
    pr_url: str | None = None
    pr_number: int | None = None
    commit_sha: str | None = None
    branch: str | None = None
    spec_doc_path: str | None = None

class Outcome(BaseModel):
    """The contract every role's CLI writes to stdout as its terminal line."""
    schema_version: Literal[1] = 1
    kind: OutcomeKind
    confidence: OutcomeConfidence
    summary: str = Field(..., max_length=500)
    findings: list[Finding] = []
    artifacts: OutcomeArtifacts = Field(default_factory=OutcomeArtifacts)
    raw_role_output_path: str | None = Field(
        None,
        description="Path to a file on disk holding the role's full reasoning trace, if too large for stdout"
    )
```

### Per-role kind matrix

| Role | Emits these `kind` values |
| --- | --- |
| Planner | `CLEAN` (spec PR open) · `NEEDS_HELP` (ticket under-specified) · `ERROR` |
| Reviewer-on-spec | `CLEAN` (approve + merge) · `NEEDS_FIX` (with findings) · `ERROR` |
| Fixer-on-spec | `CLEAN` (amended spec PR pushed) · `NEEDS_HELP` (cannot resolve) · `ERROR` |
| Worker | `CLEAN` (impl PR open) · `BLOCKED` (CI in flight) · `NEEDS_HELP` · `ERROR` |
| Reviewer-on-impl | `CLEAN` (approve + enqueue MergeQueue) · `NEEDS_FIX` · `ERROR` |
| Fixer-on-impl | `CLEAN` (amended impl PR pushed) · `NEEDS_HELP` · `ERROR` |

`BLOCKED` is the case the Poller turns into a re-poll: the state machine writes `outcome_kind="blocked"`, stays in the same logical state but advances the sequence counter, and the Poller picks it up next tick to check whether the blocking artifact has changed (CI verdict, MergeQueue verdict).

### Stdout shape

The role's stdout has two distinct sections:

1. **Human-readable trace** (everything before the marker). Rich-formatted log lines for the operator reading `foreman log --tail`.
2. **Terminal Outcome line** — a single line beginning with the marker `FOREMAN_OUTCOME:` followed by the JSON. The daemon scans stdout in reverse for the marker, parses the suffix as JSON, validates as `Outcome`.

The marker prefix keeps Outcome parsing robust to any log lines the role emits, including ones that happen to look like JSON.

### Versioning

`schema_version: 1` is the only valid value at v4 ship. Future schema changes append fields (default-valued) and bump the version. Roles never read other roles' outcomes; only the daemon's state machine consumes them. This keeps version churn contained to one parser.

### Validation failure handling

Outcome parsing happens in `state.verify(ctx, raw_stdout)`. The default `verify` implementation:

1. Scans stdout in reverse for `FOREMAN_OUTCOME:`. If missing → raise `OutcomeMissingError`.
2. Parses the suffix as JSON. If malformed → raise `OutcomeMalformedError` with the raw text.
3. Validates against `Outcome`. If validation fails → raise `OutcomeInvalidError` with the pydantic errors.

All three raise; the Template Method `transition()` catches them in the verify phase and routes to `FailedState` with `failure_phase="verify"`, `failure_reason` carrying the exception detail.

## Migration path

Clean break, no migration script:

1. **Land v4 substrate** as a single PR.
2. **Stop v3 daemon.** Don't drain — just kill. Any in-flight tickets get abandoned (their work-in-progress PRs are still on GitHub; can be manually re-triggered or left for cleanup).
3. **Delete v3 code** in the same PR (`reconciler/rules.py`, label-mutating action handlers, label-driven `_LABEL_TO_ACTION` map, the `reconciler.py` crash-recovery module).
4. **Start v4 daemon.** Fresh SQLite, new state machine, ready for new tickets.
5. **Verify** — `foreman ps` is empty initially; first new ticket flows through end-to-end.

Acceptable because foreman's ticket volume is low (low single digits in flight at any time) and Jeff is explicit that any in-flight work at cutover time is okay to lose. Cleaner than a stop-the-world migration script that has its own bug surface.

## Alternatives considered

1. **Finish the LabelManager migration on `feat/label-manager`.** Rejected. LabelManager fixes the symptom (stale labels) without addressing the architecture (labels-as-state, two-phase PR thrash). The LabelManager work is salvageable for the v4 `LabelObservabilityObserver`, but the broader substrate redesign is the higher-leverage move.
2. **Drop the multi-role pipeline; collapse to single-Worker.** Rejected. Per Jeff's brainstorm: the roles do good work; the brittleness is in coordination, not in roles. Single-Worker drops capability foreman has earned.
3. **Keep two-phase PR; just fix labels-as-state.** Rejected. Doesn't address the CI thrash from N² rebases when multiple tickets are in flight. Single-PR is the cleaner answer.
4. **Build a workflow-engine library (Temporal, etc.) instead of a hand-rolled state machine.** Rejected for v4. The state machine is small (11 states), single-host, and benefits from being legible Python in a 100-line file rather than a workflow DSL. Boring code wins.

## Open questions

- **Trigger label name.** Today `foreman:plan`. Keep, or rename to `foreman:queue` to reflect the v4 model? Worker should decide during impl PR.
- **Multi-project state isolation.** v4 SQLite has one DB shared across projects. Per-project DBs are an option for stronger isolation. Defer until v4 ships and we observe contention.
- **MergeQueue branch-protection requirements.** GitHub's MergeQueue requires specific branch-protection rules + workflow file. Document the per-repo setup checklist in `docs/RUNBOOK.md`; surface as a one-time enable step.
- **Role-side idempotency on daemon-crash-mid-execute.** Deferred per the 2026-06-13 adversarial review (C3 — "let's see if it happens"). Each role's `execute()` may have visible-to-GitHub side effects before the Outcome is written to SQLite; on daemon crash + restart, re-dispatching can produce duplicate / conflicting artifacts (extra PR comment, divergent commits, etc.). v4 ships without an explicit per-role idempotency protocol; if duplicate-artifact bugs are observed in production, the fix is a state-instance-ID stamping convention on commits/comments + per-role "is this work already done?" checks at the start of `execute()`.
- **PR draft → ready API permissions.** Need to confirm Planner-bot has permission to open as draft AND Worker-bot has permission to flip to ready, or designate one identity for the flip.

Confidence: medium. The State pattern + Mediator + Observer combination is well-understood; the unknowns are the GitHub-side mechanics (MergeQueue per-repo setup, App permission boundaries).

## Out of scope

- Distributed/multi-host daemon. v4 is single-host.
- Replacing the LLM roles or their prompts.
- Replacing PyGithub.
- Building a web UI. CLI only.
- Auto-generated reports / dashboards. The structured-log file + CLI queries are sufficient.
- Slack/Discord notification integrations. Add as observers later.
- Replacing the Docker runtime or per-ticket worktree isolation.
- Cross-project orchestration (e.g. "wait for ticket A in voice to merge before unblocking ticket B in foreman").
- Per-state retry policies beyond the existing `≤3 attempts` rule.

## References

- foreman#307 — LabelManager spec + impl-in-progress branch. v4's `LabelObservabilityObserver` borrows from the LabelManager design; the LabelManager work was the empirical motivation for v4.
- foreman#170, #160, #303 — earlier instances of the labels-as-state failure class.
- `docs/superpowers/plans/2026-06-11-foreman-architecture-stability-plan.md` — the D1-D9 decision arc that established the typed Label catalog. v4 builds on D1 (catalog stays; the runtime is what changes).
- `docs/superpowers/specs/2026-06-03-foreman-v3-declarative-reconciler-design.md` — v3 reconciler design. v4 replaces the rules engine; v3 is the immediate predecessor.
- `feat/label-manager` branch — the lifecycle test (`tests/test_label_manager_lifecycle.py`) is the empirical model v4's `FakeGitProvider`-based lifecycle test will port from.
