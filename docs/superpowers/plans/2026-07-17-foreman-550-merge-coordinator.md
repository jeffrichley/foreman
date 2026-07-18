# foreman#550 — Per-repo FIFO Merge Coordinator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serialize all merges (spec + impl) per repo through an explicit, observable, persistent FIFO queue driven by a daemon component, so intra-repo implementation concurrency (`max_in_flight` > 1) becomes safe.

**Architecture:** A new `MergeCoordinator` daemon component (sibling to the Poller/WorkerPool, ticked from `Daemon.tick_once`) owns a persistent per-repo `merge_queue` (Postgres). Both merge points (`SpecMerging`, `Merging`) enqueue and park the ticket in a new `MergeQueued` state (excluded from WorkerPool dispatch → no worker slot). The coordinator processes each repo's queue strictly serially — reusing `attempt_merge` (#317's update-branch → wait-green → merge) — routing to the post-merge state on success and via #317 on failure, with a 3-cycle poison-PR bound.

**Tech Stack:** Python 3.12, Postgres (psycopg), pydantic, pytest. Reuses #317 (`required_check_state`, `attempt_merge`, `OutcomeKind.NEEDS_FIX`) — merged.

**Spec:** `docs/superpowers/specs/2026-07-17-foreman-550-merge-coordinator-design.md`.

## Global Constraints

- ruff (google-docstrings, D-rules), mypy `--strict`, the `just check` gate (80 floor / diff-cover 80). Keep `ruff format` clean even though `just check` doesn't run format-check (#433).
- **NO `Co-Authored-By`** trailer. Conventional-commit titles, lowercase subject initial.
- Worktree is already `uv sync`'d — use `uv run --no-sync`. (Nested-worktree gotcha: never `uv run --no-project`.)
- A new `GitProvider` Protocol method must be added to **all four** implementers: Protocol, `FakeGitProvider`, `PyGithubGitProvider`, **`RoutingGitProvider`** (run unscoped `mypy packages/foreman/src`). This bit #317.
- Fakes mirror real strictly. Reuse #317's `FakeGitProvider.seed_check_state`.
- Serial only — **NO speculative batching**. FIFO only — **NO priority**. Default per-repo `max_in_flight` stays **1** (opt-in to >1).

## File Structure

- `packages/foreman/src/foreman/v4/config.py` — relax `ProjectConfig.max_in_flight` validator `le=1` → `ge=1` (default 1).
- `packages/foreman/src/foreman/v4/repository.py` — `MergeQueueEntry` dataclass + `MergeQueueRepository` methods on the Protocol + `InMemoryTicketRepository`.
- `packages/foreman/src/foreman/v4/postgres_repository.py` + `postgres_schema.sql` — the `merge_queue` table + Postgres impls.
- `packages/foreman/src/foreman/v4/states/merge_queued.py` — new `MergeQueuedState` (parked; coordinator-driven). Register in `states/registry.py`.
- `packages/foreman/src/foreman/v4/states/merging.py` + `states/spec_merging.py` — `execute()` enqueues + routes to `MergeQueued` instead of calling `attempt_merge` directly.
- `packages/foreman/src/foreman/v4/queue_manager.py` — exclude `MergeQueued` tickets from dequeue.
- `packages/foreman/src/foreman/v4/merge_coordinator.py` — NEW: the `MergeCoordinator` component + loop.
- `packages/foreman/src/foreman/v4/daemon.py` — tick the coordinator; reconcile the queue on startup.
- `packages/foreman/src/foreman/v4/cli/` — `foreman merge-queue` command.

---

### Task 1: Relax the per-repo cap (config)

**Files:** Modify `packages/foreman/src/foreman/v4/config.py`; Test `packages/foreman/tests/v4/test_config.py`.

**Interfaces:** Produces: `ProjectConfig.max_in_flight` now accepts any int ≥ 1 (default 1).

- [ ] **Step 1: Update the failing tests.** In `test_config.py`, the existing `test_project_config_max_in_flight_above_one_rejected` asserts `>1` is rejected — that is now WRONG. Rewrite it to `test_project_config_max_in_flight_above_one_accepted`:
```python
def test_project_config_max_in_flight_above_one_accepted():
    """foreman#550: the merge coordinator makes intra-repo concurrency safe,
    so a per-repo cap > 1 is now allowed. Default stays 1 (opt-in)."""
    p = ProjectConfig(name="p", repo="o/r", local_clone_path="/tmp/r", max_in_flight=3)
    assert p.max_in_flight == 3

def test_project_config_max_in_flight_defaults_to_one():
    p = ProjectConfig(name="p", repo="o/r", local_clone_path="/tmp/r")
    assert p.max_in_flight == 1
```
Keep `test_project_config_max_in_flight_zero_rejected` (ge=1 still rejects 0).

- [ ] **Step 2: Run → FAIL** (`>1` currently rejected): `uv run --no-sync pytest packages/foreman/tests/v4/test_config.py -k max_in_flight -v`.

- [ ] **Step 3: Implement.** Change the field:
```python
    max_in_flight: int = Field(default=1, ge=1)
```
and update its docstring: per-repo cap; default 1; **>1 is now safe because the merge coordinator (#550) serializes merges** — was `le=1` (hardened by the 2026-07-17 self-heal review); lifted by #550.

- [ ] **Step 4: Run → PASS.** Also `uv run --no-sync mypy packages/foreman/src/foreman/v4/config.py`.

- [ ] **Step 5: Commit** — `git commit -m "feat(v4): allow per-repo max_in_flight > 1 (default 1) — coordinator makes it safe (#550)"`.

---

### Task 2: The `merge_queue` persistence

**Files:** Modify `repository.py`, `postgres_repository.py`, `postgres_schema.sql`; Test `packages/foreman/tests/v4/test_repository.py` (or a new `test_merge_queue_repository.py`).

**Interfaces (Produces):**
```python
@dataclass(frozen=True)
class MergeQueueEntry:
    id: int
    project: str
    ticket_id: int
    pr_number: int
    kind: str        # "spec" | "impl"
    status: str      # "queued" | "merging"
    attempts: int
    enqueued_at: dt.datetime

# On TicketRepository Protocol + InMemory + Postgres:
def enqueue_merge(self, *, project: str, ticket_id: int, pr_number: int, kind: str, now: dt.datetime) -> MergeQueueEntry: ...
def merge_queue_for_project(self, project: str) -> list[MergeQueueEntry]: ...   # FIFO by enqueued_at
def head_merge_entry(self, project: str) -> MergeQueueEntry | None: ...          # earliest queued|merging
def mark_merge_active(self, entry_id: int) -> None: ...                          # status -> "merging"
def increment_merge_attempts(self, entry_id: int) -> int: ...                    # returns new count
def dequeue_merge(self, entry_id: int) -> None: ...
def list_active_merges(self) -> list[MergeQueueEntry]: ...                        # status == "merging" (for crash recovery)
```

- [ ] **Step 1: Write failing InMemory tests** in `test_merge_queue_repository.py`:
```python
def test_enqueue_and_fifo_order():
    r = InMemoryTicketRepository()
    a = r.enqueue_merge(project="p", ticket_id=1, pr_number=10, kind="impl", now=_t(1))
    b = r.enqueue_merge(project="p", ticket_id=2, pr_number=11, kind="spec", now=_t(2))
    assert [e.pr_number for e in r.merge_queue_for_project("p")] == [10, 11]
    assert r.head_merge_entry("p").id == a.id

def test_mark_active_attempts_and_dequeue():
    r = InMemoryTicketRepository()
    e = r.enqueue_merge(project="p", ticket_id=1, pr_number=10, kind="impl", now=_t(1))
    r.mark_merge_active(e.id)
    assert r.head_merge_entry("p").status == "merging"
    assert r.increment_merge_attempts(e.id) == 1
    assert [x.id for x in r.list_active_merges()] == [e.id]
    r.dequeue_merge(e.id)
    assert r.head_merge_entry("p") is None

def test_per_project_isolation():
    r = InMemoryTicketRepository()
    r.enqueue_merge(project="a", ticket_id=1, pr_number=10, kind="impl", now=_t(1))
    assert r.head_merge_entry("b") is None
```
(`_t(n)` = a fixed `dt.datetime(2026, 7, 17, 0, 0, n)`.)

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement InMemory** — add a `self._merge_queue: list[MergeQueueEntry]` + an id counter to `InMemoryTicketRepository.__init__`; implement the 7 methods (FIFO by `enqueued_at`, per-project filter). Add the same signatures to the `TicketRepository` Protocol (with docstrings).

- [ ] **Step 4: Implement Postgres.** Add to `postgres_schema.sql`:
```sql
CREATE TABLE IF NOT EXISTS merge_queue (
    id            BIGSERIAL PRIMARY KEY,
    project       TEXT NOT NULL,
    ticket_id     BIGINT NOT NULL,
    pr_number     INTEGER NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('spec','impl')),
    status        TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','merging')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    enqueued_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS merge_queue_project_order ON merge_queue (project, enqueued_at);
```
Implement the 7 methods in `postgres_repository.py` following the existing psycopg patterns in that file (parameterized queries, the connection/pool usage of the sibling methods). `head_merge_entry` = `SELECT ... WHERE project=%s ORDER BY enqueued_at ASC LIMIT 1`.

- [ ] **Step 5: Run InMemory tests → PASS.** `uv run --no-sync pytest packages/foreman/tests/v4/test_merge_queue_repository.py -v`. mypy on both repo files.

- [ ] **Step 6: Commit** — `git commit -m "feat(v4): add merge_queue persistence (repository + postgres) for the coordinator (#550)"`.

---

### Task 3: The `MergeQueued` state + hand-off + QueueManager exclusion

**Files:** Create `states/merge_queued.py`; Modify `states/registry.py`, `states/merging.py`, `states/spec_merging.py`, `queue_manager.py`; Test `test_merge_queued.py`, `test_queue_manager.py`, `test_merging.py`, `test_spec_merging.py`.

**Interfaces:**
- Consumes: `enqueue_merge` (Task 2), `TicketState` (`foreman.v4.state`).
- Produces: `MergeQueuedState` (registered as `"MergeQueued"`); merge states now enqueue + route to it.

- [ ] **Step 1: `MergeQueuedState`** — a parked state the coordinator drives; its `execute` should never be dispatched by the WorkerPool (Task's exclusion enforces that), but implement it defensively to return a BLOCKED outcome (re-park) if ever called. Follow the shape of a simple existing state (e.g. `states/queued.py`). `state_name = "MergeQueued"`. `next_state` returns `MergeQueuedState()` (self) on BLOCKED. Register in `STATE_REGISTRY` (`states/registry.py`): `"MergeQueued": MergeQueuedState`.
  - Test: `MergeQueuedState().state_name == "MergeQueued"`; registry round-trips it.

- [ ] **Step 2: Hand-off from `Merging` / `SpecMerging`.** These states currently call `attempt_merge` in `execute()`. Change `execute()` to instead **enqueue** the PR and route to `MergeQueued`:
  - Read the current `execute()`/`next_state` in each. The merge is no longer done here — `execute()` calls `ctx.repo.enqueue_merge(project=ctx.ticket.project, ticket_id=ctx.ticket.id, pr_number=<pr>, kind="impl"|"spec", now=ctx.clock())` (idempotent: if an entry for this ticket already exists, don't double-insert — check `merge_queue_for_project` for this ticket_id first) and returns an outcome whose `next_state` is `MergeQueuedState()`.
  - `Merging` → kind `"impl"`; `SpecMerging` → kind `"spec"`. Preserve the base-ref guard that exists in Merging (a bad base still → NEEDS_HELP before enqueue).
  - Tests: `Merging.execute` on a ready ticket enqueues one `impl` entry and routes to `MergeQueued`; same for `SpecMerging` with `spec`; a second execute doesn't double-enqueue.

- [ ] **Step 3: QueueManager exclusion.** In `queue_manager.py`'s dequeue filter, skip any candidate whose `state_name == "MergeQueued"` (they're coordinator-driven, not worker-driven — like the held/blocked filters already there).
  - Test: a ticket in `MergeQueued` is never returned by `dequeue()` even when a slot is free.

- [ ] **Step 4: Run the affected tests → PASS**, mypy on changed files.

- [ ] **Step 5: Commit** — `git commit -m "feat(v4): MergeQueued state + hand-off; QueueManager excludes it from dispatch (#550)"`.

---

### Task 4: The `MergeCoordinator` component + loop

**Files:** Create `merge_coordinator.py`; Modify `daemon.py` (tick it); Test `test_merge_coordinator.py`.

**Interfaces:**
- Consumes: `head_merge_entry`/`mark_merge_active`/`increment_merge_attempts`/`dequeue_merge` (Task 2); `attempt_merge` + `required_check_state` (#317); `MergeQueuedState`; `set_ticket_state` (the repo method that persists a ticket's current state — confirm its name in `repository.py`).
- Produces: `class MergeCoordinator` with `def tick(self) -> None`.

- [ ] **Step 1: Write failing coordinator tests** (`FakeGitProvider` + `seed_check_state`, `InMemoryTicketRepository`). Cover each routing cell + the bound + FIFO:
```python
def test_coordinator_merges_head_and_routes_impl_to_done(fake):
    repo, git = fake
    _seed_ticket_in_merge_queue(repo, git, project="p", ticket_id=1, pr=10, kind="impl",
                                mergeable_state="clean", ci=RequiredCheckState.PASSED)
    MergeCoordinator(repo=repo, git=git, ...).tick()
    assert repo.get_ticket(1).state_name == "Done"
    assert repo.head_merge_entry("p") is None

def test_spec_merge_routes_to_implementing(fake): ...   # kind="spec", clean/PASSED → Implementing
def test_ci_failed_routes_to_impl_fix_and_dequeues(fake): ...  # blocked+FAILED → ImplFix, dequeued
def test_dirty_routes_to_impl_fix(fake): ...
def test_pending_stays_queued_no_dequeue(fake): ...     # blocked+PENDING → still head, still merging
def test_fifo_only_head_processed(fake): ...            # two entries; only head advances per tick
def test_poison_pr_bounded_to_needs_help_after_3(fake): ...  # 3 cycles → NeedsHelp + dequeue
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement `MergeCoordinator`.** Complete logic:
```python
class MergeCoordinator:
    """Serializes merges per repo via the merge_queue. One active merge per project."""
    MAX_ATTEMPTS = 3   # foreman#546 poison-PR bound

    def __init__(self, *, repo, git, projects, clock):
        self._repo, self._git, self._projects, self._clock = repo, git, projects, clock

    def tick(self) -> None:
        for project in self._projects():          # current project names
            self._tick_project(project)

    def _tick_project(self, project: str) -> None:
        entry = self._repo.head_merge_entry(project)
        if entry is None:
            return
        if entry.status != "merging":
            self._repo.mark_merge_active(entry.id)
        ctx = self._ctx_for(entry)                # StateContext with git + the ticket
        outcome = attempt_merge(ctx, pr_number=entry.pr_number,
                                on_merge_success=self._on_merge_success(entry),
                                pre_merge_guard=None)
        if outcome.kind == OutcomeKind.CLEAN:                 # merged
            self._repo.set_ticket_state(entry.ticket_id, self._post_merge_state(entry))
            self._repo.dequeue_merge(entry.id)
        elif outcome.kind == OutcomeKind.BLOCKED:             # CI pending / update-branch cycle
            n = self._repo.increment_merge_attempts(entry.id)
            if n >= self.MAX_ATTEMPTS:
                self._repo.set_ticket_state(entry.ticket_id, "NeedsHelp")
                self._repo.dequeue_merge(entry.id)
        else:                                                 # NEEDS_FIX / NEEDS_HELP
            self._repo.set_ticket_state(entry.ticket_id, self._failure_state(entry, outcome))
            self._repo.dequeue_merge(entry.id)

    def _post_merge_state(self, entry) -> str:
        return "Implementing" if entry.kind == "spec" else "Done"

    def _failure_state(self, entry, outcome) -> str:
        if outcome.kind == OutcomeKind.NEEDS_FIX:
            return "SpecFix" if entry.kind == "spec" else "ImplFix"
        return "NeedsHelp"
```
Confirm the real names of `set_ticket_state` / `get_ticket` in `repository.py`; wire `attempt_merge`'s `on_merge_success` to the existing close-issue side-effect for impl (no-op for spec), mirroring `MergingState`/`SpecMerging`.
**Note on attempts:** only the BLOCKED (still-not-mergeable) branch increments; a merge or a routed-away failure ends the entry. So "3 cycles" = 3 ticks where the PR is still blocked/pending → NeedsHelp. If wait-green legitimately takes many ticks (CI running), that would trip the bound prematurely — so **only increment when a heal/update-branch action was actually taken**, not on plain CI-pending polls (mirror #317's `_prior_blocked_heal_count` marker discipline). Adjust: increment attempts only when `attempt_merge` reports it did an update-branch/heal this tick; a plain PENDING poll does not count.

- [ ] **Step 4: Wire into the daemon.** In `daemon.py` `__init__`, construct `self._merge_coordinator = MergeCoordinator(...)`; in `tick_once`, add `self._merge_coordinator.tick()` **inside its own try/except boundary** (per #540/I1 — a coordinator error must not kill the loop). Test: `tick_once` calls the coordinator once; a coordinator exception is isolated.

- [ ] **Step 5: Run → PASS**, mypy.

- [ ] **Step 6: Commit** — `git commit -m "feat(v4): MergeCoordinator — per-repo serial merge processing with a 3-cycle bound (#550/#546)"`.

---

### Task 5: Crash recovery for the queue

**Files:** Modify `daemon.py` (or the reconcile module the startup path uses); Test `test_merge_coordinator.py` / `test_daemon.py`.

**Interfaces:** Consumes `list_active_merges` (Task 2); the existing startup reconcile hook.

- [ ] **Step 1: Failing tests:**
```python
def test_recover_merging_entry_that_already_merged(fake):
    # entry status="merging"; PR is actually merged → ticket -> post-merge state, dequeued
def test_recover_merging_entry_not_merged_resets_to_queued(fake):
    # entry status="merging"; PR not merged → status reset to "queued", ticket unchanged
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** a `MergeCoordinator.reconcile_on_startup()` that, for each `list_active_merges()` entry, re-fetches the PR (`get_pr_state`): if merged → set ticket to `_post_merge_state` + dequeue; else set the entry back to `"queued"` (so the next tick re-processes at head). Call it from the daemon's existing `reconcile_on_startup` path, after the state-instance reconcile.

- [ ] **Step 4: Run → PASS**, mypy.

- [ ] **Step 5: Commit** — `git commit -m "feat(v4): reconcile the merge_queue on startup — crash-safe merges (#550)"`.

---

### Task 6: `foreman merge-queue` CLI (observability)

**Files:** Add a command under `packages/foreman/src/foreman/v4/cli/`; Test the CLI test dir.

**Interfaces:** Consumes `merge_queue_for_project` (Task 2). Read-only.

- [ ] **Step 1: Failing test** — invoking `merge-queue` (optionally `--project X`) renders, per repo, each entry: position, ticket, PR, kind, status, attempts; and for the active (`merging`) entry a "why waiting" line. Follow the existing CLI test pattern (a query command like `foreman ps` in `cli/`).

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** the command mirroring the existing `ps`/query command structure (rich table). Columns: `pos | project | ticket | pr | kind | status | attempts`. For the head/`merging` row, add a status detail (e.g. from `required_check_state`: "green-pending", or "update-branching", or the last outcome summary). Register it like the other cli commands.

- [ ] **Step 4: Run → PASS**, mypy.

- [ ] **Step 5: Commit** — `git commit -m "feat(cli): foreman merge-queue — inspect each repo's merge queue (#550)"`.

---

### Task 7: End-to-end integration + full gate

**Files:** Test `test_merge_coordinator_e2e.py` (or extend the coordinator tests).

- [ ] **Step 1: Integration tests** (InMemory repo + FakeGitProvider, driving the coordinator ticks directly):
```python
def test_two_impl_prs_same_repo_merge_serially():
    # enqueue two impl entries (both clean/PASSED). tick() -> only head merges + -> Done + dequeued.
    # second tick() -> second merges. Never both in one tick. No race.
def test_second_starts_only_after_first_dequeues():
    # head stuck PENDING -> second never becomes head until first resolves.
def test_poison_pr_unblocks_queue_after_bound():
    # head poison (always blocked) -> after 3 -> NeedsHelp + dequeue -> second becomes head + merges.
```

- [ ] **Step 2: Run → PASS.**

- [ ] **Step 3: Full gate.** From the worktree root: `just check`. Must be fully green (pytest, 80 floor, diff-cover 80, ruff, mypy, import-lint). Also run unscoped `uv run --no-sync mypy packages/foreman/src` to catch any `RoutingGitProvider`-style Protocol gap. Add tests for any diff-cover miss.

- [ ] **Step 4: Commit** — `git commit -m "test(v4): end-to-end merge coordinator — serial, bounded, race-free (#550)"`.

---

## Self-Review

**Spec coverage:** serial gate (no batching) ✓ (T4); all merges spec+impl ✓ (T3 both states); MergeQueued leaves worker pool ✓ (T3 exclusion); coordinator loop + post-merge routing ✓ (T4); cap le=1→ge=1 default 1 ✓ (T1); failure via #317 ✓ (T4); 3-cycle bound ✓ (T4); crash recovery ✓ (T5); FIFO ✓ (T2); observability CLI ✓ (T6); fakes reuse ✓ (throughout); tests ✓ (T7).

**Type consistency:** `MergeQueueEntry` fields + the 7 repository method signatures are identical across Protocol/InMemory/Postgres (T2) and their consumers (T3/T4/T5/T6). `kind ∈ {spec,impl}`, `status ∈ {queued,merging}` consistent. `_post_merge_state`: spec→Implementing, impl→Done (matches spec). Coordinator reuses `attempt_merge`'s existing `Outcome`/`OutcomeKind` (no new kinds).

**Open items for the implementer to confirm against code (ask if ambiguous):** the exact name of the repo method that persists a ticket's current state (`set_ticket_state`?/`update_state`?) and `get_ticket`; the current `execute()`/`next_state` bodies of `Merging`/`SpecMerging` and their base-ref guard + close-issue side-effect; the daemon's `reconcile_on_startup` hook location; the CLI command-registration mechanism. The plan gives the design + interfaces; confirm these boilerplate seams against the real files.
