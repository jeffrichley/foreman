# Spec: enforce one-open-in-flight-row-per-ticket invariant at the DB level (issue #469)

## Goal

Add a partial unique index on `state_instances (ticket_id) WHERE exited_at IS NULL` so the database — not only the PID-file single-instance guard — prevents a second open in-flight row for the same ticket. Translate the resulting `UniqueViolation` to a foreman-owned exception at the `PostgresTicketRepository` seam, consistent with the existing `UniqueViolation → TicketAlreadyExistsError` pattern. See issue [#469](https://github.com/jeffrichley/foreman/issues/469).

## Acceptance criteria

- `postgres_schema.sql` declares `CREATE UNIQUE INDEX IF NOT EXISTS uq_state_instances_one_inflight ON state_instances (ticket_id) WHERE exited_at IS NULL;`.
- A fresh DB (via `PostgresTicketRepository.__init__` running the schema) and an existing DB (via the same idempotent `IF NOT EXISTS` DDL applied on next daemon startup) both get the index without further migration tooling.
- A new `StateInstanceAlreadyOpenError(ValueError)` class is defined in `repository.py`, parallel to the existing `TicketAlreadyExistsError(ValueError)`.
- `PostgresTicketRepository.open_state_instance` catches `psycopg.errors.UniqueViolation` from the INSERT and re-raises as `StateInstanceAlreadyOpenError(str(ticket_id))` — raw psycopg exceptions do not leak.
- `InMemoryTicketRepository.open_state_instance` enforces the same invariant with an explicit pre-insert check, raising `StateInstanceAlreadyOpenError` on violation.
- The shared `RepositoryContract` test suite gains `test_open_state_instance_raises_when_already_open`, which proves both implementations raise `StateInstanceAlreadyOpenError` on a second open call for the same ticket.
- Five existing tests are updated to close each state instance before opening the next — they currently open multiple instances for the same ticket without closing, which violates the new invariant: three in `_repository_contract.py` (`test_count_consecutive_same_state_counts_run_correctly`, `test_count_consecutive_same_state_full_history_match`, `test_delete_ticket_cascades_state_instances`), one in `test_role_dispatch.py` (`test_interrupted_prior_with_matching_id_resumes`), and one in `test_transition_events.py` (`test_execute_completed_event_payload_includes_details`).
- `test_daemon_reconcile.py` gains `test_reconcile_second_run_is_noop`, which calls `reconcile_on_startup` twice and confirms the second call returns 0 (idempotent: no orphans remain after the first pass).
- `just check` exits zero.

## Approach

**Pattern (Decision 4):** No GoF pattern fits — this is straightforward "defense-in-depth at the persistence boundary" using a standard SQL partial unique index. The Google principle is **"make the right thing easy"**: the constraint fires automatically on any rogue `INSERT`, converting a silent state-corruption scenario into a loud, immediate error without any caller cooperation.

**Why the schema re-run IS the migration.** `PostgresTicketRepository.__init__` already runs `postgres_schema.sql` at every construction via `conn.execute(_SCHEMA.read_text(...))`. Every DDL statement in the schema uses `IF NOT EXISTS`, making the whole file idempotent. Adding `CREATE UNIQUE INDEX IF NOT EXISTS uq_state_instances_one_inflight ...` to the schema is therefore simultaneously a fresh-DB declaration and an existing-DB migration: the next daemon restart applies the index. If pre-existing duplicate rows somehow exist, `CREATE UNIQUE INDEX` raises a hard error — the daemon fails to start and surfaces the corruption loudly, exactly as the issue demands. No separate migration runner is needed.

**Why keep the existing non-unique `idx_state_instances_inflight`.** PostgreSQL can use a unique index for the same query-planning role as a non-unique index. Strictly speaking, `uq_state_instances_one_inflight` supersedes `idx_state_instances_inflight` (identical predicate, same column). However, dropping the non-unique index from `postgres_schema.sql` would leave it in place on any existing DB (no `DROP INDEX` is issued). Removing it cleanly is a separate operational step that is out of scope here. The Worker should leave `idx_state_instances_inflight` in the schema untouched; the two indexes coexist harmlessly.

**Exception-translation pattern.** `create_ticket` in `PostgresTicketRepository` (line 168 of `postgres_repository.py`) catches `psycopg.errors.UniqueViolation` and re-raises `TicketAlreadyExistsError`. The new code in `open_state_instance` applies the identical pattern:
```python
try:
    row = conn.execute("INSERT INTO state_instances ...", ...).fetchone()
except psycopg.errors.UniqueViolation as exc:
    conn.rollback()
    raise StateInstanceAlreadyOpenError(str(ticket_id)) from exc
```
Note: this wraps only the INSERT; the existing `TicketNotFoundError` check that precedes it is unchanged.

**InMemory parity.** `InMemoryTicketRepository.create_ticket` enforces the `TicketAlreadyExistsError` invariant with an explicit dict-lookup check before inserting. The same approach applies here: check `self._instances.values()` for an open row (`ticket_id == ticket_id and exited_at is None`) before creating the new record. This keeps the two impls contract-equivalent, which is what the shared `RepositoryContract` suite validates.

**Existing-test breakage.** Five tests across three files open two instances for the same ticket sequentially without closing the first. All must be updated.

*In `_repository_contract.py` (three tests):*
- `test_count_consecutive_same_state_counts_run_correctly` — loop calls `open_state_instance` 6 times without closing
- `test_count_consecutive_same_state_full_history_match` — loop calls `open_state_instance` 3 times without closing
- `test_delete_ticket_cascades_state_instances` — opens instance at sequence=1 then sequence=2 without closing sequence=1

Each must be updated to call `repo.close_state_instance(inst.id, now=_now())` after each open. The `count_consecutive_same_state` method counts all rows regardless of `exited_at`, so closing them does not change the count, and the test assertions remain valid.

*In `test_role_dispatch.py` (one test):*
- `test_interrupted_prior_with_matching_id_resumes` — opens `prior` at seq=1, calls `mark_execute_started` and `set_session_id` on it, then opens `current` at seq=2 while `prior` is still open.

Fix: call `repo.close_state_instance(prior.id, now=dt.datetime(2026, 6, 13))` after `repo.set_session_id(prior.id, run_id)` and before opening `current`. The `resume=True` semantic is preserved: `list_state_instances_for_ticket` returns all rows regardless of `exited_at`, `close_state_instance` sets only `exited_at` (not `execute_completed_at`), so `resolve_dispatch` still sees `prior` as an interrupted prior with the matching session id and routes to `resume=True`.

*In `test_transition_events.py` (one test):*
- `test_execute_completed_event_payload_includes_details` — the `setup` fixture opens `instance` at sequence=1; the test then opens `new_instance` at sequence=2 for state `"DiagnosticDetail"` without closing sequence=1 first.

Fix: call `repo.close_state_instance(instance.id, now=dt.datetime(2026, 6, 13))` before the `repo.open_state_instance(...)` call for `new_instance`. The test's `transition()` call and outcome assertions are unaffected — they operate entirely on `new_instance` and `ctx.bus`.

## Sub-requests (topologically sorted)

1. **Add `StateInstanceAlreadyOpenError` to `packages/foreman/src/foreman/v4/repository.py`** — insert after `TicketAlreadyExistsError`:
   ```python
   class StateInstanceAlreadyOpenError(ValueError):
       """A state instance for this ticket is already open (exited_at IS NULL)."""
   ```

2. **Add the partial unique index to `packages/foreman/src/foreman/v4/postgres_schema.sql`** — insert after the existing `idx_state_instances_inflight` block:
   ```sql
   CREATE UNIQUE INDEX IF NOT EXISTS uq_state_instances_one_inflight
       ON state_instances (ticket_id)
       WHERE exited_at IS NULL;
   ```

3. **Update `InMemoryTicketRepository.open_state_instance` in `packages/foreman/src/foreman/v4/repository.py`** — add invariant check after the existing `self.get_ticket(ticket_id)` call (line 311):
   ```python
   open_rows = [
       i for i in self._instances.values()
       if i.ticket_id == ticket_id and i.exited_at is None
   ]
   if open_rows:
       raise StateInstanceAlreadyOpenError(str(ticket_id))
   ```

4. **Update `PostgresTicketRepository.open_state_instance` in `packages/foreman/src/foreman/v4/postgres_repository.py`** — import `StateInstanceAlreadyOpenError` from `foreman.v4.repository` and wrap the INSERT in a try/except. The existing ticket-existence check is unchanged. See the exact pattern in the Approach section above.

5. **Fix five existing tests across three files that violate the new invariant:**

   **`packages/foreman/tests/v4/_repository_contract.py` (three tests):** For each of `test_count_consecutive_same_state_counts_run_correctly`, `test_count_consecutive_same_state_full_history_match`, and `test_delete_ticket_cascades_state_instances`, change the setup loops to capture the returned instance and call `repo.close_state_instance(inst.id, now=_now())` immediately after each `open_state_instance` call. The assertions in these tests are unchanged.

   **`packages/foreman/tests/v4/states/test_role_dispatch.py` — `test_interrupted_prior_with_matching_id_resumes`:** After the `repo.set_session_id(prior.id, run_id)` call and before the `repo.open_state_instance(ticket_id=ticket.id, state_name="Demo", sequence=2, ...)` call, insert:
   ```python
   repo.close_state_instance(prior.id, now=dt.datetime(2026, 6, 13))
   ```
   The `resume=True` assertion at the end of the test remains valid. `list_state_instances_for_ticket` returns all rows regardless of `exited_at` (no filter on that column), so `resolve_dispatch` still sees `prior` in the run. `close_state_instance` sets only `exited_at`, not `execute_completed_at`, so `prior` still reads as an interrupted attempt. The stored session id on `prior` still matches the derived id, so `resolve_dispatch` returns `resume=True` exactly as before.

   **`packages/foreman/tests/v4/test_transition_events.py` — `test_execute_completed_event_payload_includes_details`:** Before the `repo.open_state_instance(ticket_id=ticket.id, state_name="DiagnosticDetail", sequence=2, ...)` call inside the test body, insert:
   ```python
   repo.close_state_instance(instance.id, now=dt.datetime(2026, 6, 13))
   ```
   where `instance` is the seq=1 row opened by the `setup` fixture. The `_DiagnosticDetailState().transition(ctx_diag)` call and the outcome-detail assertions are unaffected — they operate entirely on `new_instance` and `ctx.bus`.

6. **Add `StateInstanceAlreadyOpenError` to the imports at the top of `packages/foreman/tests/v4/_repository_contract.py`** — add it to the existing `from foreman.v4.repository import (...)` block.

7. **Add `test_open_state_instance_raises_when_already_open` to `packages/foreman/tests/v4/_repository_contract.py`**:
   ```python
   def test_open_state_instance_raises_when_already_open(self, repo: TicketRepository) -> None:
       """A second open_state_instance call for a ticket that already has an open
       row must raise StateInstanceAlreadyOpenError (one-in-flight-per-ticket
       invariant: DB partial unique index / InMemory explicit check)."""
       t = repo.create_ticket(project="p", issue_number=1, now=_now())
       repo.open_state_instance(
           ticket_id=t.id, state_name="Planning", sequence=1, now=_now()
       )
       with pytest.raises(StateInstanceAlreadyOpenError):
           repo.open_state_instance(
               ticket_id=t.id, state_name="Planning", sequence=2, now=_now()
           )
   ```

8. **Add `test_reconcile_second_run_is_noop` to `packages/foreman/tests/v4/test_daemon_reconcile.py`**:
   ```python
   def test_reconcile_second_run_is_noop():
       """Closing orphans then re-running reconcile finds none (idempotent)."""
       now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
       repo = InMemoryTicketRepository()
       t = repo.create_ticket(project="p", issue_number=1, now=now)
       repo.open_state_instance(
           ticket_id=t.id, state_name="Planning", sequence=1, now=now
       )
       first_run = reconcile_on_startup(repo, clock=lambda: now)
       assert first_run == 1
       second_run = reconcile_on_startup(repo, clock=lambda: now)
       assert second_run == 0
       assert repo.list_in_flight_state_instances() == []
   ```

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/v4/postgres_schema.sql` | Add `CREATE UNIQUE INDEX IF NOT EXISTS uq_state_instances_one_inflight ON state_instances (ticket_id) WHERE exited_at IS NULL;` after the existing `idx_state_instances_inflight` block |
| `packages/foreman/src/foreman/v4/repository.py` | Add `StateInstanceAlreadyOpenError(ValueError)` after `TicketAlreadyExistsError`; add invariant check in `InMemoryTicketRepository.open_state_instance` |
| `packages/foreman/src/foreman/v4/postgres_repository.py` | Import `StateInstanceAlreadyOpenError`; wrap INSERT in `open_state_instance` with `try/except psycopg.errors.UniqueViolation` |
| `packages/foreman/tests/v4/_repository_contract.py` | Import `StateInstanceAlreadyOpenError`; fix 3 existing tests (close instances before re-opening); add `test_open_state_instance_raises_when_already_open` |
| `packages/foreman/tests/v4/test_daemon_reconcile.py` | Add `test_reconcile_second_run_is_noop` |
| `packages/foreman/tests/v4/states/test_role_dispatch.py` | In `test_interrupted_prior_with_matching_id_resumes`: close `prior` (seq=1) before opening `current` (seq=2) to satisfy the new invariant; `resume=True` semantic is preserved |
| `packages/foreman/tests/v4/test_transition_events.py` | In `test_execute_completed_event_payload_includes_details`: close `instance` (seq=1, from `setup` fixture) before opening `new_instance` (seq=2, `"DiagnosticDetail"`) |

## Alternatives considered

1. **Apply the index via a dedicated migration script / Alembic.** Introducing a migration framework (Alembic, yoyo-migrations) adds a dependency and a new operational concept (migration state table, version tracking). Rejected: the existing `IF NOT EXISTS` idempotent schema re-run already serves as a lightweight migration mechanism for additive DDL changes (the pattern is already established). Adding a framework for a single one-line index change is overkill.

2. **Enforce the invariant in application code only (no DB constraint).** An explicit `SELECT 1 FROM state_instances WHERE ticket_id = %s AND exited_at IS NULL` check in `PostgresTicketRepository.open_state_instance` before the INSERT, with no index. Rejected: TOCTOU race — two daemons (the exact failure scenario the issue targets) can both pass the check before either issues the INSERT. The DB constraint is the only atomically safe guard. Application-code checks on InMemory are for test parity, not primary enforcement.

3. **Use a DB-level trigger instead of a partial unique index.** A `BEFORE INSERT` trigger that counts open rows and raises an exception. Rejected: triggers are procedural, harder to test, invisible to `psql \d state_instances`, and provide no query-planning benefit. A partial unique index is declarative, self-documenting, enforced by the planner, and idiomatic PostgreSQL.

## Open questions

None. The schema DDL, exception class, and exception-translation pattern are all precisely specified by the issue and confirmed against the codebase.

## Out of scope

- Removing the existing non-unique `idx_state_instances_inflight` index (superseded by the new unique index but harmless to leave in place; clean removal requires a `DROP INDEX` DDL outside this change).
- Changing the single-instance PID-file guard in `v4/cli/daemon.py` — this is defense-in-depth underneath that guard, not a replacement.
- Raising `max_in_flight` above 1 — this change is a prerequisite for that future work but does not itself change the concurrency limit.
- Checking `exc.diag.constraint_name` to distinguish the `uq_state_instances_one_inflight` violation from the `UNIQUE (ticket_id, sequence)` violation in `open_state_instance` — sequence duplicates are a programming error caught in dev; the simple `UniqueViolation` catch is consistent with the existing `create_ticket` pattern.
