# Foreman Crash Recovery — Stage 1a: Reconciliation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daemon restart (incl. Watchtower redeploy) must never falsely escalate a healthy ticket to NeedsHelp, and must never leak orphaned in-flight `state_instances` rows.

**Architecture:** A startup reconciliation pass closes orphaned in-flight rows with `failure_phase="crash_recovery"`; that phase is exempted from the runaway-cap counter so recovered crashes don't count as failures. Re-derives state from the journal (no flags). Single-instance daemon (PID lock) + reconcile-before-first-tick guarantees every in-flight row at startup is an orphan.

**Tech Stack:** Python 3.12, `TicketRepository` Protocol (InMemory / SQLite / Postgres impls + shared contract suite), pytest. TDD.

**Scope:** This plan is **Stage 1a** (the C1 reconciliation fix) only. Stage 1b (the I1 healer/observe-before-act duplicate-PR guard) and Stage 2 (session resume) are separate plans. Design: `docs/superpowers/specs/2026-06-25-foreman-crash-recovery-design.md`.

---

## File structure

- `packages/foreman/src/foreman/v4/records.py` — add `FAILURE_PHASE_CRASH_RECOVERY` constant (single source of truth; avoids the magic-string-across-repos smell, review I3).
- `packages/foreman/src/foreman/v4/repository.py` — InMemory `count_consecutive_same_state`: exempt crash_recovery.
- `packages/foreman/src/foreman/v4/sqlite_repository.py` — same exemption.
- `packages/foreman/src/foreman/v4/postgres_repository.py` — same exemption.
- `packages/foreman/src/foreman/v4/reconcile.py` — **new**: `reconcile_on_startup(repo, *, clock) -> int`.
- `packages/foreman/src/foreman/v4/daemon.py` — call reconcile once at the top of `run_forever`, before the tick loop.
- Tests: `packages/foreman/tests/v4/_repository_contract.py` (exemption parity), `packages/foreman/tests/v4/test_reconcile.py` (**new**), `packages/foreman/tests/v4/test_daemon_reconcile.py` (**new**, wiring).

---

### Task 1: Exempt `crash_recovery` from the runaway-cap counter

A reconciled orphan has `failure_phase="crash_recovery"`, `outcome_kind=None`. `count_consecutive_same_state` skips only `can_run` / `BLOCKED` / `TRANSIENT_PROVIDER_ERROR`, so today the orphan counts → false escalation. (`count_consecutive_transient_provider_errors` already skips `outcome_kind is None`, so it needs no change — assert that.)

**Files:**
- Modify: `packages/foreman/src/foreman/v4/records.py`
- Modify: `repository.py:428`, `sqlite_repository.py:435`, `postgres_repository.py:456`
- Test: `packages/foreman/tests/v4/_repository_contract.py`

- [ ] **Step 1: Add the contract test (drives all three impls)**

In `_repository_contract.py`, add to the shared contract suite:

```python
def test_crash_recovery_rows_are_exempt_from_consecutive_same_state(self, repo):
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    ticket = repo.create_ticket(project="p", issue_number=1, now=now)
    # Three crash-orphans on the same state, each closed by reconciliation:
    for seq in (1, 2, 3):
        inst = repo.open_state_instance(
            ticket_id=ticket.id, state_name="Implementing", sequence=seq, now=now,
        )
        repo.record_failure(
            inst.id, now=now,
            failure_phase="crash_recovery", failure_reason="daemon crash",
        )
        repo.close_state_instance(inst.id, now=now)
    # All three are crash_recovery → none count toward the cap.
    assert repo.count_consecutive_same_state(
        ticket_id=ticket.id, state="Implementing",
    ) == 0
    # And the transient counter (already None-outcome-skipping) is also 0.
    assert repo.count_consecutive_transient_provider_errors(ticket.id) == 0
```

- [ ] **Step 2: Run it; verify it FAILS for all three impls**

Run: `uv run pytest packages/foreman/tests/v4/_repository_contract.py -k crash_recovery -v`
Expected: FAIL — `count_consecutive_same_state` returns 3, not 0 (the orphans count).

- [ ] **Step 3: Add the shared constant**

In `records.py` (imported by all repos), add near the top-level constants:

```python
#: failure_phase value written by the startup reconciliation pass to a
#: crash-orphaned in-flight row. Exempt from count_consecutive_same_state
#: (a daemon restart is not a ticket failure). Single source of truth so
#: the four sites that reference it can't drift (review I3).
FAILURE_PHASE_CRASH_RECOVERY = "crash_recovery"
```

- [ ] **Step 4: Exempt it in all three impls**

`repository.py` — in `count_consecutive_same_state`, after the `can_run` skip (line ~439):

```python
            if inst.failure_phase == FAILURE_PHASE_CRASH_RECOVERY:
                # A daemon restart closed this orphan; it is not
                # runaway-defense signal. Skip (neither count nor break).
                continue
```

`postgres_repository.py` — identical line after its `can_run` skip (it walks `StateInstanceRecord`s).

`sqlite_repository.py` — same, using the row dict:

```python
                if row["failure_phase"] == FAILURE_PHASE_CRASH_RECOVERY:
                    continue
```

Add `from foreman.v4.records import FAILURE_PHASE_CRASH_RECOVERY` to each file that doesn't already import it.

- [ ] **Step 5: Run the contract test; verify PASS for all impls**

Run: `uv run pytest packages/foreman/tests/v4/_repository_contract.py -k crash_recovery -v`
Expected: PASS (InMemory + SQLite + Postgres if available).

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/records.py packages/foreman/src/foreman/v4/repository.py packages/foreman/src/foreman/v4/sqlite_repository.py packages/foreman/src/foreman/v4/postgres_repository.py packages/foreman/tests/v4/_repository_contract.py
git commit -m "feat(v4): exempt crash_recovery rows from the runaway-cap counter"
```

---

### Task 2: `reconcile_on_startup()` — close orphaned in-flight rows

**Files:**
- Create: `packages/foreman/src/foreman/v4/reconcile.py`
- Test: `packages/foreman/tests/v4/test_reconcile.py`

- [ ] **Step 1: Write the failing test**

```python
import datetime as dt
from foreman.v4.reconcile import reconcile_on_startup
from foreman.v4.repository import InMemoryTicketRepository

def test_reconcile_closes_orphans_as_crash_recovery():
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    repo = InMemoryTicketRepository()
    t = repo.create_ticket(project="p", issue_number=1, now=now)
    inst = repo.open_state_instance(
        ticket_id=t.id, state_name="Implementing", sequence=1, now=now,
    )  # left in-flight, as a crash would
    assert repo.list_in_flight_state_instances()  # precondition

    recovered = reconcile_on_startup(repo, clock=lambda: now)

    assert recovered == 1
    assert repo.list_in_flight_state_instances() == []          # closed
    closed = [i for i in repo.list_state_instances_for_ticket(t.id) if i.id == inst.id][0]
    assert closed.failure_phase == "crash_recovery"
    assert closed.exited_at is not None

def test_reconcile_noop_when_no_orphans():
    repo = InMemoryTicketRepository()
    assert reconcile_on_startup(repo, clock=lambda: dt.datetime(2026,1,1,tzinfo=dt.UTC)) == 0
```

- [ ] **Step 2: Run it; verify it fails (module missing)**

Run: `uv run pytest packages/foreman/tests/v4/test_reconcile.py -v`
Expected: FAIL — `ModuleNotFoundError: foreman.v4.reconcile`.

- [ ] **Step 3: Implement `reconcile.py`**

```python
"""Startup reconciliation for crash-orphaned in-flight state instances.

When the daemon dies mid-transition the Template Method's ``finally`` never
runs, leaving a ``state_instances`` row open (``exited_at IS NULL``) that no
process is executing. This pass — run ONCE at daemon startup, before the
WorkerPool starts a single thread (single-instance daemon, PID-locked) — finds
those orphans and closes each as ``crash_recovery``. That phase is exempt from
the runaway-cap counter, so a restart never escalates a healthy ticket.

Re-derives from the journal; carries no flags. The Poller re-enqueues the
ticket at its unchanged ``current_state`` on the first tick, as it already does.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from foreman.v4.records import FAILURE_PHASE_CRASH_RECOVERY
from foreman.v4.repository import TicketRepository

logger = logging.getLogger(__name__)


def reconcile_on_startup(
    repo: TicketRepository, *, clock: Callable[[], dt.datetime]
) -> int:
    """Close every orphaned in-flight row as crash_recovery. Returns the count.

    Idempotent: a second run finds no in-flight rows (the first closed them),
    so re-invoking is a no-op.
    """
    orphans = repo.list_in_flight_state_instances()
    now = clock()
    for inst in orphans:
        repo.record_failure(
            inst.id, now=now,
            failure_phase=FAILURE_PHASE_CRASH_RECOVERY,
            failure_reason=(
                f"daemon restart: state {inst.state_name!r} was in-flight "
                f"(instance {inst.id}) when the previous process exited"
            ),
        )
        repo.close_state_instance(inst.id, now=now)
    if orphans:
        logger.warning(
            "crash recovery: closed %d orphaned in-flight state instance(s)",
            len(orphans),
        )
    return len(orphans)
```

- [ ] **Step 4: Run the tests; verify PASS**

Run: `uv run pytest packages/foreman/tests/v4/test_reconcile.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/reconcile.py packages/foreman/tests/v4/test_reconcile.py
git commit -m "feat(v4): reconcile_on_startup closes crash-orphaned in-flight rows"
```

---

### Task 3: Wire reconciliation into the daemon startup (before the tick loop)

Must run on `daemon start` (top of `run_forever`), NOT in `bootstrap_cli_context` — the latter is built by every CLI command (`foreman ps`, `foreman show`), and reconciliation must fire only when the daemon actually starts processing, once, before any worker thread.

**Files:**
- Modify: `packages/foreman/src/foreman/v4/daemon.py`
- Test: `packages/foreman/tests/v4/test_daemon_reconcile.py`

- [ ] **Step 1: Write the failing test**

```python
import datetime as dt
from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.repository import InMemoryTicketRepository
# (use the existing test fakes for git/dispatcher/pollers — see test_daemon* siblings)

def test_run_forever_reconciles_before_ticking(make_daemon):
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    repo = InMemoryTicketRepository()
    t = repo.create_ticket(project="p", issue_number=1, now=now)
    repo.open_state_instance(ticket_id=t.id, state_name="Implementing", sequence=1, now=now)
    daemon = make_daemon(repo=repo)   # fixture builds Daemon with fake pollers/git/dispatcher
    daemon.stop()                     # set the stop flag so run_forever exits after one pass
    daemon.run_forever()
    assert repo.list_in_flight_state_instances() == []   # orphan closed at startup
```

(If no `make_daemon` fixture exists, construct the `Daemon` inline mirroring `test_daemon_*`'s setup; the key assertion is that `run_forever` closed the orphan.)

- [ ] **Step 2: Run it; verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_daemon_reconcile.py -v`
Expected: FAIL — orphan still in-flight (reconcile not wired).

- [ ] **Step 3: Add a `_reconcile_startup` method + call it first in `run_forever`**

In `daemon.py`, add the import and method, and call it at the very top of `run_forever` before the `while` loop:

```python
from foreman.v4.reconcile import reconcile_on_startup

    def _reconcile_startup(self) -> None:
        """Close crash-orphaned in-flight rows left by a previous process.
        Runs once, before the first tick, before any worker thread starts."""
        reconcile_on_startup(self._repo, clock=self._clock)

    def run_forever(self) -> None:
        """Main loop. Returns when ``stop()`` is called."""
        self._reconcile_startup()
        try:
            while not self._stop.is_set():
                self.tick_once()
                self._stop.wait(self._config.tick_seconds)
        finally:
            self.shutdown(wait=True)
```

- [ ] **Step 4: Run the test; verify PASS**

Run: `uv run pytest packages/foreman/tests/v4/test_daemon_reconcile.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/daemon.py packages/foreman/tests/v4/test_daemon_reconcile.py
git commit -m "feat(v4): run startup reconciliation before the daemon tick loop"
```

---

### Task 4: End-to-end guard test + `just check` gate

Prove the headline scenario: a healthy ticket survives repeated daemon restarts without a false NeedsHelp escalation.

**Files:**
- Test: `packages/foreman/tests/v4/test_daemon_reconcile.py` (add)

- [ ] **Step 1: Write the scenario test**

```python
def test_repeated_restarts_do_not_escalate_healthy_ticket():
    """Three crash/restart cycles on the same state must NOT trip the
    max_state_attempts=3 cap, because each orphan is closed as
    crash_recovery and exempted from the counter."""
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    repo = InMemoryTicketRepository()
    t = repo.create_ticket(project="p", issue_number=1, now=now)
    for seq in (1, 2, 3):
        repo.open_state_instance(ticket_id=t.id, state_name="Implementing", sequence=seq, now=now)
        reconcile_on_startup(repo, clock=lambda: now)   # simulate restart N
    assert repo.count_consecutive_same_state(ticket_id=t.id, state="Implementing") == 0
```

- [ ] **Step 2: Run it; verify PASS**

Run: `uv run pytest packages/foreman/tests/v4/test_daemon_reconcile.py -v`
Expected: PASS.

- [ ] **Step 3: Full gate**

Run: `just check`
Expected: green — ruff + mypy + **import-linter (R1/R2)** + full pytest (incl. the Postgres contract run if configured) + coverage ≥ 78%.

- [ ] **Step 4: Commit**

```bash
git add packages/foreman/tests/v4/test_daemon_reconcile.py
git commit -m "test(v4): repeated restarts don't falsely escalate a healthy ticket"
```

---

## Self-review checklist (run before handing off)

1. **Spec coverage:** reconciliation closes orphans (Task 2) ✓; crash_recovery exempt from cap (Task 1) ✓; runs once before workers (Task 3) ✓; no false escalation across restarts (Task 4) ✓. Healer guard (I1) and resume (Stage 2) are explicitly out of this plan.
2. **No placeholders:** all code shown; the only `make_daemon` hedge points at existing sibling-test setup.
3. **Type consistency:** `FAILURE_PHASE_CRASH_RECOVERY` referenced identically in all four sites; `reconcile_on_startup(repo, *, clock)` signature consistent across def + 3 call/test sites.

## Not in this plan (follow-ons)

- **Stage 1b — healer guard (I1):** observe-before-act on PR-creating states (Planner→spec PR, Worker→impl PR) via `find_open_pr_by_head_branch` so a re-run after a crash adopts the existing PR instead of opening a duplicate.
- **Stage 2 — session resume:** Task 0 volume mount + startup assertion; `session_id` stamp; `resolve_dispatch` healer; `execute_started_at` routing; adversarial cross-role mixing tests; `output_format`-on-resume validation gate.
