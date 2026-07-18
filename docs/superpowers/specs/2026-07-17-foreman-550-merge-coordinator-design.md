# foreman#550 — Per-repo FIFO merge coordinator (ADR-0)

**Status:** design approved 2026-07-17 (Jeff, brainstorm). Ready for an implementation plan.
**Depends on:** #317 (granular merge-state routing + the `required_check_state` check-run signal) — MERGED. Realizes #546 (convergence bound) concretely. Subsumes foreman#316 (auto-rebase-on-BEHIND). Relates to #547 (fix_reason directive), #548 (SpecFix symmetry).

## Problem

`ProjectConfig.max_in_flight` is pinned to **1 per repo** (validator `le=1`). That per-repo serialization is the only thing keeping the merge states correct: they assume no *other* same-repo PR merges into a PR's base mid-flight — the BEHIND/rebase race (`states/merging.py`, foreman#316), which the cap-1 sidesteps rather than handles. #539 lifted the GLOBAL cap so DIFFERENT repos run in parallel; the per-repo cap can't lift until the same-repo merge race is actually handled.

The Implementation phase (minutes of Claude work per ticket) is the throughput bottleneck; merging is fast (git + a green-wait). So the win is **parallel implementation within a repo, with merges serialized** so they can't race.

## Approach (decided)

A **serial merge gate**, not a merge train. **No speculative batching** (YAGNI at a 3-repo, shallow-queue scale). The gate is an **explicit, observable, first-class per-repo FIFO merge queue** — a component *inside the daemon*, not a hidden lock and not a separate service. (Rationale: this session's self-heal review existed because "why is this stuck?" required log-spelunking; a hidden per-repo mutex would recreate that opacity on the merge path. An inspectable queue is the "what would Google do, scaled to us" answer — the explicitness/observability, without the speculative-execution machinery.)

**All merges are coordinated per repo** — spec-PR merges (`SpecMerging`) and impl-PR merges (`Merging`) alike, since both merge into the same base and race the same way. They already share `attempt_merge`, so there is one natural choke point.

## Architecture & data flow

**New component — `MergeCoordinator`** (a daemon sibling to the Poller and WorkerPool; ticks in the same loop). Owns a **persistent per-repo FIFO merge queue**:

`merge_queue` (Postgres): `project, ticket_id, pr_number, kind ∈ {spec, impl}, enqueued_at, status ∈ {queued, merging}, attempts` (int, for the bound).

**Entry points → one queue.** Both merge points enqueue and hand the ticket off:
- `SpecMerging` (spec PR approved) and `Merging` (impl PR approved) transition the ticket to a new **`MergeQueued`** state and insert a `merge_queue` row (`kind` = spec/impl).
- A `MergeQueued` ticket is **excluded from WorkerPool dispatch** (the QueueManager does not dequeue it) — so it consumes **no worker slot**. Implementation concurrency (worker slots) and merge serialization (the queue) become separate resources.

**Coordinator loop — per repo, one active at a time:**
1. Take the head `queued` entry → mark `merging`.
2. Run the existing `attempt_merge` sequence: update-branch onto base → wait-green via #317's `required_check_state` → merge.
3. **Success:** dequeue; route the ticket to its post-merge state — **spec-merged → Implementing; impl-merged → Done**.
4. **Failure:** dequeue; route the ticket via #317 (`dirty` → SpecFix/ImplFix with the resolve-conflict directive; CI-failed → fix; `action_required`/persistent → NEEDS_HELP), then process the next queued entry.

The merge work is git + GitHub API in the coordinator's tick — no Claude subprocess, like the Poller's sweep.

## The cap change (the payoff)

`ProjectConfig.max_in_flight` validator relaxes from `le=1` to **`ge=1`** (any ≥1 allowed — now safe because the coordinator serializes merges). **Default stays 1 per repo** — operators opt *in* to intra-repo implementation concurrency by raising the config per project. Conservative rollout: the machinery ships and makes >1 safe, but nobody is forced off serial-per-repo by default. Still bounded by the global cap (`FOREMAN_MAX_IN_FLIGHT`, currently 4).

## Failure handling & the convergence bound (#546)

All failure routes reuse #317 (above). The specific new safety is the **poison-PR bound**: the head PR holds the repo's single merge slot while its CI greens (inherent to serial). A PR whose base keeps advancing or whose CI keeps flaking would re-update-branch → re-wait → forever, wedging every PR behind it. So each queue entry carries `attempts`: after **3** update-branch→green cycles, the coordinator gives up → routes the ticket to **NEEDS_HELP** and **dequeues it**, unblocking the queue. A stuck PR can never block a repo's merges indefinitely. (This realizes #546 concretely; supersedes the retry-cap-exemption gap that let BLOCKED self-loop.)

## Crash recovery

The queue is in Postgres → survives restart. On startup the coordinator reconciles the single `merging` entry per repo: re-fetch the PR — if it merged (crash landed between merge and dequeue), route the ticket to its post-merge state + dequeue; if not, reset `merging → queued` and re-process at head. Rides the existing crash-recovery pattern (reconcile orphaned in-flight state). Merge ops are idempotent (already-merged → CLEAN; behind → re-detected), so re-processing is safe.

## Ordering

Strict FIFO by `enqueued_at`. Priority (P0 jumps the line) is a deliberate future option — out of scope now.

## Observability

`foreman merge-queue [--project X]` — renders each repo's queue: position, ticket, PR, kind, status, and for the active entry **why** it's waiting ("green-pending on windows-latest, attempt 2/3", "update-branching", "conflict → routed to ImplFix"). Plus events/logs on enqueue / merge-start / green-wait / merge-success / failure-routing. This is the "see why it's stuck in one command" the self-heal review demanded — applied to the merge path.

## Testing

Reuses #317's fakes (`FakeGitProvider` + `seed_check_state`) so the coordinator is fully deterministic (no real GitHub):
- **Unit:** the coordinator loop, one test per failure route (conflict→fix, CI-fail→fix, action_required→NeedsHelp); FIFO ordering; the bound **terminates** (poison PR → NEEDS_HELP + dequeue after 3); crash-recovery reconciliation (mid-merge restart → correct resolution, both branches: merged / not-merged).
- **Integration:** two+ tickets ready in one repo → merges serialized, no race; the second starts only after the first merges or fails-and-dequeues.
- **Config:** `max_in_flight > 1` now accepted (was rejected by `le=1`); default is 1.
- **Observability:** `foreman merge-queue` renders queued / merging / blocked states correctly.

## Out of scope

- Speculative batching / merge trains (scale feature; YAGNI).
- Priority ordering (FIFO only for now).
- A separate merge-queue service (it's a daemon component).
- #547 (ImplFix reads `fix_reason`) and #548 (SpecMerging→SpecFix symmetry) — tracked separately; this design assumes #317's current routing.

## Success criteria

- Two impl (or spec) PRs ready in one repo merge **serially and safely** — no two same-repo merges race; each is tested against the up-to-date base at merge time.
- A repo's `max_in_flight` can be set >1 and multiple tickets implement concurrently, with merges funneled through the coordinator.
- A poison PR is bounded (3 cycles → NEEDS_HELP) and never wedges the queue.
- `foreman merge-queue` shows, in one command, what's queued and why the active merge is waiting.
- The daemon can crash mid-merge and recover the queue correctly.
