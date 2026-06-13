# Foreman v4 — session snapshot (2026-06-13)

Handoff document for resuming the v4 substrate redesign work after a context compaction. Read this file first before continuing.

## Where we are

The brainstorm + spec are **complete**. The implementation plan is **outline-only** — Phase headers are in place; bite-sized TDD task blocks for each phase still need to be authored.

## Authoritative artifacts

| Path | State | Purpose |
|---|---|---|
| `docs/superpowers/specs/2026-06-13-foreman-v4-substrate-redesign-design.md` | **Done, approved by Jeff.** | The full design spec — state machine, single-PR + MergeQueue, webhook ingestion, typer CLI, durability + crash-recovery, clean-break migration. Source of truth for everything downstream. |
| `docs/superpowers/plans/2026-06-13-foreman-v4-substrate-redesign-implementation.md` | **Outline only.** 38 lines: header + 10-phase outline. | Needs ~40 bite-sized TDD task blocks across 10 phases. Each task = file paths + failing test code + impl code + commit command. |
| `docs/superpowers/specs/2026-06-13-foreman-v4-SESSION-SNAPSHOT.md` | This file. | Handoff for resumption. |

## Branch state

| Branch | State | Notes |
|---|---|---|
| `feat/label-manager` (off `main`) | 4 commits pushed to GitHub. LabelManager + lifecycle test live. **Superseded by v4** — Jeff approved scrapping this in favor of the substrate rewrite. | Can be left as-is; v4 will absorb the LabelManager work into the `LabelObservabilityObserver`. Do NOT continue merging the LabelManager call sites — v3 is being deleted. |
| `foreman/issue-307` | The original spec PR branch (#308). 4 spec-amendment commits + 3 v4 spec/plan commits sit on this branch (the v4 work was committed here for convenience; in retrospect it should have been on a fresh branch). | If v4 work proceeds: rebase the v4 commits onto a fresh `feat/foreman-v4-substrate` branch off main, then close PR #308 with a note pointing at v4. |
| `main` | Current `main` HEAD includes PR #305 (vulture allowlist), PR #306 (AdminConfig deletion — #303), PR #311 (import-linter — #309). Daemon running on this version. | No v4 work merged yet. |

## v4 spec — design summary (so the compaction has a self-contained record)

**Goal:** Replace foreman's label-as-state coordination substrate. Keep all roles (Planner / Reviewer-on-spec / Fixer / Worker / Reviewer-on-impl / Fixer-on-impl) and the `needs-help` escalation. Replace the substrate beneath them.

**Architecture:**

- **State pattern** with five-hook lifecycle per state: `can_run` (preverify), `enter` (setup), `execute` (work), `verify` (postverify), `exit` (teardown). Template Method `transition()` on the abstract `TicketState` orchestrates them with distinct failure handlers per phase.
- 11 concrete states: `Queued`, `Planning`, `SpecReview`, `SpecFix`, `Implementing`, `ImplReview`, `ImplFix`, `Merging`, `Done`, `Failed`, `NeedsHelp`.
- **Mediator `QueueManager`** owns the work queue, dispatch order, concurrency caps. Producers and the Worker Pool talk through it, never directly.
- **Two producers** feeding events to the QueueManager:
  - **`WebhookReceiver`** (FastAPI + uvicorn) exposed via **tailscale funnel** for public reach. Verifies HMAC-SHA256 signatures, normalizes GitHub webhook payloads (`issues.labeled`, `pull_request.*`, `check_suite.completed`, `workflow_run.*`, `merge_group.*`) into domain Events, dedups by delivery ID.
  - **`ReconciliationPoller`** runs at slower cadence for downtime catch-up. Queries `state_instances WHERE exited_at IS NULL`, polls only those artifacts.
- **Observer pattern** routes side effects: `SQLitePersistenceObserver`, `LabelObservabilityObserver` (writes one `foreman:state-X` label, write-only), `StructuredLogObserver`, `MetricsObserver` (no-op stub for now).
- **Repository pattern** over SQLite (`TicketRepository` Protocol + `InMemoryTicketRepository` for tests + `SqliteTicketRepository` for prod). Two tables: `tickets` (with `held_by`/`held_at`/`held_reason` columns for operator hold) and `state_instances` (the journal — every state's lifecycle leaves a row with `entered_at`, `execute_started_at`, `execute_completed_at`, `exited_at`, `outcome_kind`, `outcome_payload`, `next_state`, `failure_phase`, `failure_reason`).
- **Crash recovery** is one query: `SELECT * FROM state_instances WHERE exited_at IS NULL`. Three resume cases per timestamp combination (mid-execute, between states, during exit). Roles are idempotent so re-dispatch is safe.
- **Operator pause/resume** = `held_by` flag on the ticket row, NOT a state change. In-flight `execute()` is allowed to complete; pause takes effect at the next state boundary.
- **Single PR per ticket** (draft during spec phase → ready during impl phase → enqueued in GitHub MergeQueue for the actual merge). Eliminates O(N²) CI thrash from N parallel PRs.
- **MergeQueue** is the default merge mechanism for autonomous-loop PRs. Foreman just enqueues; GitHub serializes the rebase + CI + merge dance.
- **Typer CLI** (replacing Click): `ps`, `show`, `log`, `queue`, `daemon start/stop/reload`, `hold`, `resume`, `retry`, `skip`, `drop`, `set-state`, plus direct role invocations (`plan`, `review`, `fix`, `implement`). Output uses `rich.Table` (ps) / `rich.Tree` (show) / `rich.Live` (log --tail).
- **Rich logging**: `RichHandler` for stdout (colored, level-highlighted), `JsonLinesHandler` for file persistence.
- **Roles** keep their prompts + logic. The only change is their CLI exit emits a structured `Outcome` JSON on stdout (`{"kind": "clean", "confidence": "high", "findings": [...], "artifacts": {"pr_url": "..."}}`) instead of writing labels. The State Machine reads stdout, parses, decides next state.
- **Migration**: clean break. Kill v3, delete `reconciler/rules.py` + label-mutating action handlers + the v3 `reconciler.py` crash-recovery module in the same PR. No migration script. Any in-flight tickets at cutover are abandoned (low volume, Jeff accepted the loss).

**Patterns named explicitly:** State, Template Method, Mediator, Observer, Repository, Strategy (CLI output formatting), Command (queue work).

**SOLID principles applied** at class AND method granularity (Jeff's explicit request).

**Open questions in the spec:**

1. Trigger label name: keep `foreman:plan` or rename to `foreman:queue` to reflect the v4 model? Worker decides during impl.
2. Multi-project state isolation: one SQLite DB shared across projects (current plan), or per-project DBs?
3. MergeQueue per-repo branch-protection requirements: documented in `docs/RUNBOOK.md` as setup checklist.
4. Tailscale funnel URL stability: confirm the daemon's funnel URL is stable across restarts; if not, document the rotation procedure.
5. Webhook secret rotation: per-repo HMAC secrets; rotation procedure to document.

## Plan progress: what's done

The `2026-06-13-foreman-v4-substrate-redesign-implementation.md` file contains:

- **Header**: goal, architecture, tech stack, commit cadence, source-of-truth pointer.
- **10-phase outline**: each phase named with completion criterion mapped to spec sections.

The phases are:

1. **Foundation** — Repository + Outcome + TicketState ABC + Template Method `transition()`. Tasks would create: `v4/__init__.py`, `v4/outcome.py`, `v4/repository.py`, `v4/schema.sql`, `v4/storage.py`, `v4/state.py`, plus matching tests.
2. **Events + Observers** — `Event` base, `EventBus`, 4 observer impls.
3. **Concrete states** — 11 state subclasses, end-to-end lifecycle test against `FakeGitProvider` (port the test pattern from `feat/label-manager`'s `test_label_manager_lifecycle.py`).
4. **QueueManager + Commands** — Mediator + Command pattern.
5. **Webhook ingestion** — FastAPI receiver, HMAC verification, payload normalization, dedup.
6. **ReconciliationPoller** — downtime catch-up.
7. **Role-side Outcome reporting** — modify each of the 4 role CLI entry points to emit JSON.
8. **Typer CLI** — operator command set + rich output.
9. **Rich logging + MergeQueue default** — `RichHandler`, `JsonLinesHandler`, `DaemonConfig.merge_mechanism = "queue"`.
10. **v3 deletion + cutover docs** — delete `rules.py`, label-mutating actions, v3 reconciler; add per-repo webhook + MergeQueue setup checklist to RUNBOOK.

## Plan progress: what's NEEDED

For each of ~40 tasks across the 10 phases, the plan needs:

- Exact file paths (`Create:` / `Modify:` lines).
- A failing test (full code block).
- The expected fail message when the test runs.
- The minimal impl code (full code block) to make the test pass.
- The expected pass output.
- A commit command (conventional commit, lowercase subject, specific staged files).

This is ~25-40 KB of plan content. Phase 1's 5 tasks alone are ~22 KB (I drafted them this session but the heredoc was too long for the bash CLI; the content was lost when the command failed). They can be re-authored in a fresh session.

## How to resume

**Option A — fresh session writes the plan tasks.** Read the spec + this snapshot. Then incrementally write each phase's tasks via the Write tool (small append-per-phase to avoid CLI length issues). After plan is complete, invoke `superpowers:subagent-driven-development` to execute.

**Option B — skip the detailed plan, go straight to SDD against the spec.** Riskier (subagents make more granular decisions on the fly) but faster. The spec's "Sub-requests" + "File-level changes" sections give SDD enough scaffolding to chunk work itself.

Jeff's lean is option A (proper plan first, then SDD), but option B is viable if he wants velocity.

## Key constraints to preserve

- Use Wren PAT (pulled via `python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py --being wren get github --keyring --password`). Pass via `GH_TOKEN` env var only; **NEVER** echo or bus the token.
- Stage specific files (no `git add -A`).
- Conventional commits, lowercase subjects.
- Local git config `user.name=wrenrichley`, `user.email=wrenrichley@gmail.com`.
- Prefer Bash over PowerShell for git operations on Windows.
- Pre-push hook runs `just check` (ruff + mypy + import-linter + pytest) — must stay green.
- Adversarial review before every PR (per `feedback_adversarial_review_before_pr` memory).
- Standing authorization to send bus envelopes to Pepper without asking.
- Don't push every commit (Jeff: "you don't have to push every time. faster if you don't").

## Discord delivery

The spec was delivered to Jeff's DM (channel `1508863457762480289`). Discord-wren accepts `ToolInvocation` with `tool=discord_send` and args `{channel_id, text, files}` (NOT `attachments`, NOT `content`). HMAC-validated.
