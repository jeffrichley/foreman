# Foreman v4 Crash Recovery — Design

**Date:** 2026-06-25 · **Author:** Wren (brainstormed with Jeff)
**Status:** design, pending review → implementation plan
**Source findings:** `docs/architecture-review-2026-06-25.md` (C1/I1),
`docs/architecture-review-2026-06-25-independent.md` (C1 confirmed independently),
`docs/superpowers/spikes/2026-06-25-sdk-session-resume.md` (resume validated).

## Problem

The v4 daemon journals each state's execution as a `state_instances` row: a row
is *opened* (`exited_at IS NULL`) when a ticket enters a state and *closed* in
the Template Method's `finally` when the state finishes. If the daemon dies hard
mid-`execute()` (OOM, host reboot, `docker kill`, **Watchtower redeploy**), the
`finally` never runs and the row is **orphaned** — it says "this state is
running" but nothing is running it.

Two consequences (architecture-review C1):

1. **False escalation.** `count_consecutive_same_state()` counts rows to detect a
   stuck ticket and escalate to NeedsHelp after `max_state_attempts` (default 3).
   Its skip-set is only `{can_run, BLOCKED, TRANSIENT_PROVIDER_ERROR}` — a crash
   orphan (NULL `failure_phase`, NULL `outcome_kind`) matches none, so it
   **counts**. A few crash/restart cycles on one ticket silently escalate a
   *healthy* ticket to NeedsHelp, though no role ever failed.
2. **Leak.** Orphan rows are never closed; they accumulate in the
   `idx_state_instances_inflight` partial index and distort every future count.

A second, deeper hazard (I1): on restart the Poller re-enqueues the ticket and
the role **re-runs from scratch**. If the crash happened *after* the role pushed
a PR but *before* the journal advanced, the re-run can create a **duplicate PR**.
The merge states are immune (they re-derive from GitHub — the "healer" pattern);
the role-dispatch states are not.

`list_in_flight_state_instances()` already exists on the `TicketRepository`
Protocol and both impls — and has **zero production callers**. The recovery hook
was built and never wired.

## Goals / non-goals

**Goals.** A daemon restart — including a Watchtower redeploy mid-work — must:
- never falsely escalate a healthy ticket to NeedsHelp;
- never leak orphan in-flight rows;
- never create a duplicate PR/comment by re-running an interrupted role;
- (Stage 2) recover *efficiently* by resuming the role's Claude session rather
  than re-doing expensive work — without ever crossing sessions between roles.

**Non-goals.** Multi-instance/HA daemon (single instance assumed). Mid-turn
*partial* resumability finer than the Claude session boundary. Changing the
`max_in_flight=1` model (separate finding I4).

## Governing principles

1. **Re-derive from reality, don't carry flags.** The merge states are crash-safe
   because `attempt_merge` re-reads GitHub every time. Recovery extends the same
   discipline: decisions are re-derived from the journal / GitHub at the point of
   action, not stored as "please resume" intentions.
2. **Fail safe to *fresh*.** A fresh role run is always safe (worst case: redo
   work, caught by the healer). A *wrong* resume is unrecoverable. Every resume
   decision biases hard to fresh — resume only on an exact, verified match.
3. **Single instance + reconcile-before-work.** A PID lock guarantees one daemon.
   Reconciliation runs once at startup *before the WorkerPool starts a thread*, so
   every in-flight row that exists at that moment is, by definition, an orphan
   from the dead process.

## Two-stage delivery

The correctness line falls between the stages: **Stage 1 makes crashes correct
using only primitives that already exist; Stage 2 makes them cheap.**

### Stage 1 — Reconciliation + healer guard (correctness; no volume mount, no resume)

Ships the C1 + I1 fix as one PR. Two pieces:

**1a. Startup reconciliation.** A `reconcile_on_startup()` pass, invoked from
`bootstrap_cli_context` (or the top of `Daemon.run_forever`, before the first
tick). For each row from `list_in_flight_state_instances()`:
- record a synthetic failure with `failure_phase = "crash_recovery"` and a
  distinct reason;
- `close_state_instance()` it (sets `exited_at`, drops it from the in-flight
  index).
Then add `"crash_recovery"` to the skip-set in **both**
`count_consecutive_same_state()` and `count_consecutive_transient_provider_errors()`
(in all repo impls), so recovered crashes never count toward any cap. The Poller
then re-enqueues the ticket at `current_state` as it already does — now cleanly.

**1b. Healer guard on the role-dispatch states.** Before a role-dispatch state
creates an artifact, observe reality: `find_open_pr_by_head_branch()` (already on
`GitProvider`) — if the prior (crashed) attempt already opened the PR for this
ticket's head branch, do not open a second one; adopt the existing PR and
advance. This closes the duplicate-PR window using the same observe-before-act
shape as `attempt_merge`.

After Stage 1: a crash re-runs **safely** (no false escalation, no leak, no
duplicate PR). It is *wasteful* (re-does work) but correct. Nothing new is
required — `list_in_flight_state_instances` and `find_open_pr_by_head_branch`
already exist.

### Stage 2 — Resume arm (efficiency)

Makes the safe re-run *cheap* by continuing the role's Claude session instead of
restarting it. Resume is a **healer-shaped decision at the dispatch step**, not a
component that runs on its own. Three collaborating pieces:

**Task 0 (prerequisite).** Mount a persistent volume at the Claude session dir
(`CLAUDE_CONFIG_DIR=/root/.claude-container`, currently ephemeral — see the
spike) **+ a startup assertion** that the dir resolves onto a mount, so
resumability can never be silently lost again. Without this, transcripts are
wiped on every container recreate (the common Watchtower case).

**2a. Stamp a scoped `session_id` at dispatch.** When a role-dispatch state
dispatches, it sets `ClaudeAgentOptions.session_id` to an id **bound to the work
identity** `(ticket_id, role, target)` — e.g. a deterministic
`uuid5(ns, f"{ticket}:{role}:{target}:{run_key}")` — and persists it on the
`state_instances` row. Different role/target/ticket → different id *by
construction*; no Reviewer dispatch can compute a Planner's id.

**2b. `resolve_dispatch(ctx) → fresh | resume(session_id)`** — a new shared
helper in the role-dispatch path, analogous to `attempt_merge` for the merge
states. It reads the journal and decides:
- Look up the most recent prior attempt for this **exact** `(ticket, role,
  target)` within the **current consecutive-same-state run**.
- Resume **only if** that attempt's recorded identity matches exactly **and** it
  was *interrupted* (`execute_started_at` set, never completed). → `resume=<id>`.
- Otherwise — no prior, state changed, completed session, any mismatch, any doubt
  — **fresh**.
- The `SubprocessRoleDispatcher.dispatch()` gains a `resume` param it forwards to
  the SDK (`ClaudeAgentOptions.resume`), which forwards it to `claude --resume`.

**Routing by `execute_started_at`** (the crash-timing split):
- orphan with `execute_started_at IS NULL` → role never started → **fresh** (zero
  side-effect risk);
- orphan with `execute_started_at` set → role was running → **resume** arm.

**Unit of resumability = the consecutive-same-state run**, *not* the raw
`state_instance_id` (which changes on each retry: orphan row X closed, new row Y
opened). Same state retried after a crash → resume X's session from Y. State
*changes* (Planning → SpecReview) → no prior same-state session in this run →
fresh. This is the rule that stops a *completed* Planning session from ever
bleeding into later work.

**Bound the resume attempts.** Mirroring `MAX_HEAL_ACTIONS`: if resume keeps
failing (a poison session), after N resume attempts fall back to a fresh run, and
if *that* keeps failing the existing `max_state_attempts` cap escalates to
NeedsHelp. Resume must never loop forever.

## Anti-mixing safeguards (cross-role session safety)

A wrong-role resume is catastrophic (an agent loaded with the wrong role's entire
context). Three independent walls, governed by fail-safe-to-fresh:

1. **Scoped id by construction** — `(ticket, role, target)` → unique id; no
   cross-ticket / cross-role / cross-target collision is computable.
2. **Verify-or-fresh** — `resolve_dispatch` resumes only on an exact identity
   match of an interrupted same-work attempt; any mismatch → fresh. No
   positional/"latest session" path exists.
3. **cwd wall** — Claude stores transcripts under
   `…/projects/<munged-cwd>/<session-id>.jsonl`; foreman worktrees are per-ticket
   and target-aware, so the storage directory is itself role/target-scoped. And
   an explicit `session_id` is *always* passed, so Claude never guesses which
   session in a directory to resume. *(Confirm worktrees are keyed per
   `(ticket,target)` during implementation — if so the filesystem itself can't
   cross sessions.)*

## Failure modes (after the design)

| Failure | Outcome |
|---|---|
| Crash before `execute()` started | orphan closed (crash_recovery, not counted); re-run fresh; zero side effects |
| Crash mid-`execute()`, no PR yet | Stage 1: re-run fresh. Stage 2: resume → continues. No duplicate. |
| Crash after PR pushed, before journal advance | Stage 1 healer: adopt existing PR, no duplicate. Stage 2: resume sees its own work. |
| Repeated crashes on one ticket | crash_recovery rows exempt → no false NeedsHelp |
| Resume itself fails repeatedly (poison session) | bounded → fall back to fresh → `max_state_attempts` escalates |
| Wrong-role / wrong-ticket / completed session candidate | verify-or-fresh → fresh (never a wrong resume) |
| Container recreate wipes session dir | Task 0 volume mount + startup assertion prevents silent loss |

## Testing

- Reconciliation: orphan rows closed + `crash_recovery` exempt from both counters
  (contract-tested across InMemory/SQLite/Postgres).
- Healer guard: a pre-existing open PR on the head branch → no second PR.
- **Adversarial mixing tests (load-bearing):** attempt to resume with a wrong
  role, wrong ticket, wrong target, and a *completed* session — assert every one
  falls back to fresh.
- Routing: `execute_started_at` NULL → fresh; set → resume.
- Resume bound: N failing resumes → fresh → escalation.
- Validation gate: a **resumed** role run still emits the structured
  `FOREMAN_OUTCOME` (untested in the spike — must pass before Stage 2 ships).

## Open questions for review

1. `session_id` derivation — deterministic `uuid5` (recomputable, no storage) vs.
   capture-and-store the assigned id? (Leaning deterministic; still store it on
   the row for auditability.)
2. Worktree scoping — confirm per-`(ticket,target)` so the cwd wall holds.
3. Resume-attempt bound value (e.g. 1–2 before fresh) — pick during planning.
4. Does the Stage-1 healer guard belong on *all* role-dispatch states or only the
   PR-creating ones (Planner/Worker)? (Reviewer/Fixer mutate an existing PR.)

## Out of scope (tracked separately)

- `max_in_flight=1` correctness dependency (review I4).
- Per-project concurrency isolation (review I1-availability).
- Worker-future silent-crash recording (review I2).
