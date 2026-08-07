# Spec: drain lease closes gate-update race between idle check and SIGTERM (issue #599)

## Goal

Close the race condition documented in [#599](https://github.com/jeffrichley/foreman/issues/599): `foreman gate-update` observes board idleness but does not hold it. Between the command exiting 0 and the SIGTERM arriving (~3 s), the daemon's polling loop can open a new state-instance — the very work a redeploy would orphan. The fix adds a PostgreSQL-backed **drain lease**: `gate-update` acquires the lease before re-checking in-flight work; `open_state_instance` in both repository implementations refuses to insert a new row while an unexpired lease is active.

## Acceptance criteria

- [ ] With the drain lease active, calling `repo.open_state_instance(...)` raises `DrainLeaseActiveError` — no new state-instance row is inserted.
- [ ] `gate-update` releases the drain lease when it **defers** (exits 75), so a single busy poll does not silently block dispatch until the TTL expires.
- [ ] `gate-update` leaves the drain lease set when it returns **idle** (exits 0), so no new state-instance can open in the window before SIGTERM arrives.
- [ ] The drain lease has a TTL (default 300 s). An expired lease is treated as absent — a failed or abandoned update never permanently wedges dispatch (fail-open preserved).
- [ ] All three existing exit paths in `test_gate_update.py` continue to pass.
- [ ] `_repository_contract.py` gains contract tests for `acquire_drain_lease`, `release_drain_lease`, and the `DrainLeaseActiveError` guard in `open_state_instance`; both `InMemoryTicketRepository` and `PostgresTicketRepository` pass them.
- [ ] `just check` exits zero.

## Approach

**Pattern (Decision 4):** No GoF pattern fits cleanly. The applicable principle is **OCP + repository-layer enforcement**: the `TicketRepository` abstraction is extended with drain-lease methods without any change to `WorkerPool` or `QueueManager` — callers of `open_state_instance` don't need to know about the lease. The drain lease is a **critical section guard**: `gate-update` acquires it, re-checks the shared state, and either releases it (busy) or holds it (idle). This is the canonical "check-then-act" pattern made safe by acquiring the lock before the check, not after.

### Why the flag must precede the re-check

The issue's own explanation is correct and must be preserved:

> Step 2 must come after step 1, or the race simply moves.

If `gate-update` checked in-flight first and THEN set the lease, the dispatcher could open a state-instance in the gap between the check and the lease insertion. By inserting the lease FIRST and checking in-flight SECOND, any dispatcher call to `open_state_instance` that arrives AFTER the lease is inserted is blocked — even if the QueueManager already dequeued the item.

### Where the lease lives

`gate-update` runs as a **separate process** invoked by Watchtower inside the daemon container. It has its own `PostgresTicketRepository` with its own connection pool. The only shared state between the gate process and the daemon process is the PostgreSQL database. The lease MUST be a database row; an in-memory flag cannot work.

A new singleton table `drain_lease` in `postgres_schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS drain_lease (
    id          INTEGER PRIMARY KEY DEFAULT 1,
    acquired_at TIMESTAMPTZ NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    CHECK (id = 1)
);
```

`UPSERT ON CONFLICT (id) DO UPDATE` makes `acquire_drain_lease` idempotent and handles the re-use case where a prior lease exists but has expired.

### Where dispatch is blocked

`PostgresTicketRepository.open_state_instance` checks the lease **inside the same connection** (same transaction) as the existence check and INSERT. This is the right boundary because:

1. `WorkerPool` calls `open_state_instance` as the first real work in `_run_transition`, before any subprocess is started. Blocking here prevents the subprocess from ever launching.
2. Enforcement at the repository layer means `WorkerPool`, `QueueManager`, and every state module remain unchanged — only the persistence layer knows about the lease.

`InMemoryTicketRepository.open_state_instance` adds the identical guard (checking `_drain_lease_expires_at` against `now`) so the contract suite covers both implementations.

The exception raised is `DrainLeaseActiveError(LookupError)`, defined in `repository.py` alongside the other domain errors. When `WorkerPool._run_transition` sees it, the existing `_escalate_crashed_transition` path parks the ticket in `NeedsHelp` via `set_ticket_state` — the same path any other unexpected exception in `_run_transition` takes. This is safe: the SIGTERM will arrive within seconds, the daemon will restart, and `crash_recovery` will close any open instances exactly as it did in the race shown in the issue.

### Modified `cmd_gate_update` flow

```
1. repo.acquire_drain_lease(now=now, ttl_seconds=300)
2. in_flight = repo.list_in_flight_state_instances()
3. if in_flight:
       repo.release_drain_lease()
       raise typer.Exit(code=75)
4. # idle: leave lease set, fall through → exit 0
```

The fail-open `except` block wraps the entire sequence (including `acquire_drain_lease`). If the repo raises at any point, the behavior is unchanged: exit 0 + WARNING to stderr. A failed acquisition means no lease was set, so dispatch is not blocked — the worst case is the existing pre-fix race, not a wedged board.

When `release_drain_lease` itself raises on the deferred path, the exception is swallowed (best-effort) rather than turning a valid defer into a fail-open exit-0.

### TTL and fail-open preservation

TTL = 300 s (5 minutes). Watchtower's timeout for lifecycle hooks defaults to 60 s. If `gate-update` exits 0 and the daemon never receives a SIGTERM (hook ran at the wrong time, Watchtower bug), the lease expires automatically within 5 minutes and dispatch resumes unblocked — the existing fail-open property is preserved.

An expired lease in `open_state_instance` is treated as absent: `expires_at <= now` → proceed normally.

### RUNBOOK correction

The "Watchtower idle-gate" table in `docs/RUNBOOK.md` (line 375) currently says "≥1 open (non-terminal) ticket" which was the original #412 behavior. The implementation was already corrected to in-flight state-instances (foreman#412 review). Update the table to reflect the current behavior and add a paragraph documenting the drain lease and what to do if the lease is stuck (wait for TTL, or `foreman gate-update --force-release` is out of scope; the operator escape hatch is unchanged).

## Sub-requests (topologically sorted)

1. **Add `drain_lease` table to `postgres_schema.sql`**: the singleton table with `id INTEGER PRIMARY KEY DEFAULT 1`, `acquired_at TIMESTAMPTZ NOT NULL`, `expires_at TIMESTAMPTZ NOT NULL`, and `CHECK (id = 1)`. Use `CREATE TABLE IF NOT EXISTS` to match the existing pattern.

2. **Add `DrainLeaseActiveError` to `repository.py`**: new exception class inheriting from `LookupError`, alongside `TicketNotFoundError` / `StateInstanceNotFoundError`. Add three new methods to the `TicketRepository` Protocol: `acquire_drain_lease(*, now: dt.datetime, ttl_seconds: int = 300) -> None`, `release_drain_lease() -> None`, and `is_drain_lease_active(*, now: dt.datetime) -> bool`.

3. **Implement drain lease in `InMemoryTicketRepository`** (`repository.py`): add `_drain_lease_expires_at: dt.datetime | None = None` to `__init__`. Implement `acquire_drain_lease`, `release_drain_lease`, `is_drain_lease_active`. Modify `open_state_instance` to check `is_drain_lease_active(now=now)` and raise `DrainLeaseActiveError` if true — check must come BEFORE the ticket-existence check.

4. **Implement drain lease in `PostgresTicketRepository`** (`postgres_repository.py`): implement `acquire_drain_lease` (UPSERT into `drain_lease`), `release_drain_lease` (DELETE WHERE id=1), `is_drain_lease_active` (SELECT + compare `expires_at > now`). Modify `open_state_instance` to run `SELECT expires_at FROM drain_lease WHERE id = 1` inside the same connection as the insert; if the row exists and `_from_db_required(lease["expires_at"]) > now`, rollback and raise `DrainLeaseActiveError`.

5. **Rewrite `cmd_gate_update`** (`v4/cli/gate_update.py`): replace the single `try/except` with the four-step flow described in Approach. The `except Exception` wraps `acquire_drain_lease` + `list_in_flight_state_instances`. Release the lease on defer; swallow any release failure. Update the module docstring to describe the drain lease.

6. **Extend `_repository_contract.py`**: add three contract tests — `test_drain_lease_blocks_open_state_instance`, `test_drain_lease_releases_correctly`, `test_expired_drain_lease_does_not_block`. These run against both `InMemoryTicketRepository` and `PostgresTicketRepository`.

7. **Extend `test_gate_update.py`**: add `test_gate_update_idle_leaves_lease_active` (repo has no in-flight work → gate exits 0 → `repo.is_drain_lease_active(now=now)` is True) and `test_gate_update_busy_releases_lease` (repo has in-flight work → gate exits 75 → `repo.is_drain_lease_active(now=now)` is False).

8. **Update `docs/RUNBOOK.md`** "Watchtower idle-gate" subsection: correct the table row from "≥1 open (non-terminal) ticket" to "≥1 in-flight role job (open state-instance)"; add a "Drain lease" paragraph explaining the lease, TTL, and that `is_drain_lease_active` can be queried via `psql` (`SELECT * FROM drain_lease;`) to diagnose a stuck board.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/v4/postgres_schema.sql` | Add `drain_lease` singleton table. |
| `packages/foreman/src/foreman/v4/repository.py` | Add `DrainLeaseActiveError`; add three drain-lease methods to `TicketRepository` Protocol; add drain-lease state and methods to `InMemoryTicketRepository`; guard `InMemoryTicketRepository.open_state_instance` with lease check. |
| `packages/foreman/src/foreman/v4/postgres_repository.py` | Add `acquire_drain_lease`, `release_drain_lease`, `is_drain_lease_active` implementations; guard `open_state_instance` with lease check (same connection). |
| `packages/foreman/src/foreman/v4/cli/gate_update.py` | Rewrite `cmd_gate_update` with four-step drain-lease flow; update module docstring. |
| `packages/foreman/tests/v4/_repository_contract.py` | Add three drain-lease contract tests. |
| `packages/foreman/tests/v4/cli/test_gate_update.py` | Add `test_gate_update_idle_leaves_lease_active` and `test_gate_update_busy_releases_lease`. |
| `docs/RUNBOOK.md` | Correct in-flight description; add drain-lease paragraph under "Watchtower idle-gate". |

## Alternatives considered

1. **In-memory flag in the daemon process:** Set a Python `threading.Event` or module-level flag when the daemon receives SIGTERM. Rejected: `gate-update` is a separate process invoked by Watchtower. It shares no memory with the daemon. Any cross-process coordination must go through the database.

2. **Postgres advisory lock instead of a table:** `pg_try_advisory_lock(key)` is lighter-weight and auto-releases on connection drop. Rejected: advisory locks are session-scoped — a connection borrowed from the pool and immediately returned would release the lock. Simulating a persistent advisory lock across psycopg_pool connections requires holding a connection open for the duration of the update window, which fights the pool design. A row with a TTL is explicit, auditable, and matches how the rest of the persistence layer works.

3. **Check only in `QueueManager.dequeue`:** Block dequeue when a drain lease is active so items never reach `WorkerPool._run_transition`. Rejected: `QueueManager` doesn't have database access (by design — it holds only the in-memory heap and calls `repo.get_ticket`/`repo.list_unmet_dependencies`). Adding a lease check there would require injecting yet another repo method into QM or adding a lease-aware dequeue parameter. Enforcing at `open_state_instance` is cleaner and catches work dequeued before the lease was set.

4. **Re-check in-flight before lease acquisition (inverted order):** Check in-flight first, then acquire lease. Rejected explicitly by the issue: "Step 2 must come after step 1, or the race simply moves." The dispatcher can open a state-instance in the gap between the check and the lease insert, reproducing the original race at a different moment.

## Open questions

None. The approach is fully grounded in verified code: `gate_update.py` (current implementation), `postgres_repository.py` (schema + connection patterns), `repository.py` (Protocol + InMemory shapes), `worker_pool.py` (`_run_transition` calling `open_state_instance`), `_repository_contract.py` (contract test structure), and `test_gate_update.py` (existing test shape and fixtures).

## Out of scope

- SIGTERM drain (foreman#598): waiting for in-flight work to finish after SIGTERM. Independent fix; the issue comment notes #598 should land first. This spec does not depend on or conflict with #598.
- Operator CLI command to force-release a stuck lease (e.g. `foreman gate-update --force-release`). Operators can release via `psql`: `DELETE FROM drain_lease;`. A CLI wrapper is a convenience follow-up.
- Per-project drain leases. A global lease is correct: Watchtower stops the entire daemon container, not per-project subprocesses.
- Changes to the Watchtower polling interval or hook timeout configuration.
