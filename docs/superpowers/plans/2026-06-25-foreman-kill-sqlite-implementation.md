# Foreman — Kill SQLite — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Remove SQLite from foreman entirely — the repository impl, the SQLite snapshot/backup subsystem, and the spent SQLite→Postgres cutover tool — leaving Postgres as the only production backend and `InMemoryTicketRepository` as the test double.

**Architecture:** Production already runs Postgres (the `foreman-postgres` compose sidecar). SQLite lingers only as (a) the de-facto test backend, (b) a `.db`-file snapshot scheduler that no longer protects authoritative state, and (c) a one-time cutover tool. Swap tests to the already-contract-passing `InMemoryTicketRepository`, delete the three SQLite subsystems, and make the storage selector Postgres-only with a **loud fail** on misconfiguration. Postgres DR backup is out of scope (tracked: jeffrichley/foreman#434).

**Tech Stack:** Python 3.12, Pydantic v2, typer CLI, pytest (incl. live Postgres via `postgres_fixture`), `just check` gate. Deletion-heavy: the verification is "suite stays green + grep shows zero SQLite references", not new behavior.

**Spec:** `docs/superpowers/specs/2026-06-25-foreman-kill-sqlite-design.md`

**Key code refs (verified):**
- Storage selector: `bootstrap.py:58-69` (`if config.storage.engine == "postgres": … else: SqliteTicketRepository.at_path(...)`); imports at `bootstrap.py:32-33`.
- Backup wiring: `bootstrap.py:160-213` (constructs `backup_scheduler`, passes it to `Daemon(...)`); `daemon.py:33-45` (import + `_DISABLED_BACKUP_SCHEDULER` singleton), `:94` (kwarg), `:125` (store), `:176` (`self._backup_scheduler.tick()`).
- Config: `config.py:257-281` `BackupConfig`; `:284-306` `StorageConfig` (`engine: Literal["sqlite","postgres"] = "sqlite"` + `_dsn_required_for_postgres` validator); `:330-372` `V4Config` (`db_path: str` at :332, `backup: BackupConfig` field at :372).
- Events: `events.py:117` `BackupTakenEvent`, `:132` `BackupFailedEvent`.
- CLI registration: `cli/__init__.py:34,47` (imports), `:92` `restore`, `:94` `migrate-v4-to-v5`.
- Test double: `repository.py:188` `InMemoryTicketRepository` — **no-arg constructor** (`InMemoryTicketRepository()`).
- Test-backend usages: 93 occurrences of `SqliteTicketRepository` across 22 test files (`SqliteTicketRepository.in_memory()` is the dominant form).

---

## File structure

- **Delete (src):** `sqlite_repository.py`, `state_backup.py`, `schema.sql`, `cli/migrate.py`, `cli/restore.py`.
- **Delete (tests):** `tests/v4/test_sqlite_repository.py`, `tests/v4/test_state_backup.py`, `tests/v4/test_migrate_v4_to_v5.py`, `tests/v4/cli/test_restore.py`.
- **Update (src):** `bootstrap.py` (storage selector + backup wiring), `daemon.py` (backup scheduler removal), `config.py` (`BackupConfig` + `StorageConfig` + `db_path`), `events.py` (2 backup events), `cli/__init__.py` (deregister 2 commands), `repository.py` (docstring), the storage section of `docker/foreman/config.toml.template`.
- **Update (tests):** ~18 wiring test files (swap `SqliteTicketRepository.in_memory()` → `InMemoryTicketRepository()`), `tests/v4/_repository_contract.py` (drop the lone SQLite ref), any `_minimal_v4_config`-style builders that set `db_path` / `backup`.
- **Docs:** `docs/runbooks/postgres-migration.md` (+ any sibling) — mark the cutover historical and note SQLite is fully removed.

---

### Task 1: Swap the test backend to `InMemoryTicketRepository`

Do this FIRST so the suite stays runnable while later tasks delete the SQLite impl.

**Files:** the ~18 wiring test files that use `SqliteTicketRepository.in_memory()` (NOT the 4 SQLite-specific test files — those are deleted in Tasks 3–4); `tests/v4/_repository_contract.py`.

- [ ] **Step 1: Enumerate the swap set**

Run: `git grep -l "SqliteTicketRepository" -- packages/foreman/tests`
Expected: the 22 files from recon. Set aside the 4 that get *deleted* later (`test_sqlite_repository.py`, `test_state_backup.py`, `test_migrate_v4_to_v5.py`, `cli/test_restore.py`) — do NOT edit those. The rest are the swap set.

- [ ] **Step 2: Confirm the InMemory contract binding exists**

Run: `git grep -l "InMemoryTicketRepository" -- packages/foreman/tests`
The shared contract (`_repository_contract.py`) must run against InMemory independently of the SQLite binding (which is deleted in Task 4). Verify a concrete InMemory binding exists (e.g. `tests/v4/test_in_memory_repository.py`). If the only InMemory coverage rode on the SQLite binding, add an explicit InMemory binding that mixes in the contract — mirror the existing SQLite binding's class shape, constructing `InMemoryTicketRepository()`.

- [ ] **Step 3: Swap construction + imports in the wiring tests**

In each swap-set file: replace `SqliteTicketRepository.in_memory()` → `InMemoryTicketRepository()`, and the import `from foreman.v4.sqlite_repository import SqliteTicketRepository` → `from foreman.v4.repository import InMemoryTicketRepository`. These tests use the repo as an opaque `TicketRepository`; the constructor swap is behavior-neutral (both pass the same contract). If any test calls a SQLite-only method (none expected — they use the Protocol surface), stop and report it.

- [ ] **Step 4: Run the swapped suite**

Run: `uv run pytest packages/foreman/tests/v4 -o addopts="" -q`
Expected: green. The SQLite impl still exists (deleted in Task 4); only its test *usage* moved. A failure here means a test relied on SQLite-specific semantics rather than the contract — fix it to use the contract surface.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/tests/v4
git commit -m "test: swap test backend from SqliteTicketRepository to InMemoryTicketRepository"
```

---

### Task 2: Postgres-only storage selector with loud fail

**Files:** `config.py`, `bootstrap.py`, `docker/foreman/config.toml.template`, any `_minimal_v4_config` test builders.

- [ ] **Step 1: Write the failing test for loud-fail-on-unset**

In `tests/v4/test_config.py` (or the existing config test module — `git grep -l "StorageConfig\|V4Config(" packages/foreman/tests`), add:

```python
def test_storage_unset_engine_loud_fails_without_dsn():
    """No [storage] block (or engine unset) must NOT silently fall back to
    SQLite — it must raise a clear error demanding a Postgres DSN."""
    import pytest
    from pydantic import ValidationError
    from foreman.v4.config import StorageConfig

    with pytest.raises(ValidationError, match="dsn is required"):
        StorageConfig()  # no dsn → postgres-only selector must reject
```

Run: `uv run pytest packages/foreman/tests/v4/test_config.py -k loud_fail -o addopts="" -v`
Expected: FAIL today (StorageConfig defaults to `engine="sqlite"`, which needs no dsn).

- [ ] **Step 2: Make `StorageConfig` Postgres-only**

In `config.py` `StorageConfig` (284-306): change `engine: Literal["sqlite", "postgres"] = "sqlite"` → `engine: Literal["postgres"] = "postgres"`. Keep the `_dsn_required_for_postgres` validator — with the new default it now fires whenever `dsn` is unset, which IS the loud fail. Update the class docstring to drop the SQLite description.

- [ ] **Step 3: Remove `db_path` (now dead) from `V4Config`**

`db_path` (config.py:332) was consumed only by the deleted `SqliteTicketRepository.at_path(...)` call. Confirm: `git grep -n "db_path" -- packages/foreman/src`. If the only remaining producer/consumer is the storage selector (changed in Step 5) + config plumbing, delete the `db_path: str` field. Update every `_minimal_v4_config` / `_build_v4_config` test builder that sets `db_path=...` to drop it (`git grep -l "db_path" -- packages/foreman/tests`). If grep shows a non-SQLite consumer, stop and report rather than delete.

- [ ] **Step 4: Update the config template**

In `docker/foreman/config.toml.template`: ensure the `[storage]` block sets `engine = "postgres"` + `dsn = ...` and remove any `db_path` line. (The running daemon already sets `FOREMAN_STORAGE_ENGINE=postgres` via compose; this keeps the template self-consistent.)

- [ ] **Step 5: Make the bootstrap selector Postgres-only**

In `bootstrap.py:58-69`: delete the `else: repo = SqliteTicketRepository.at_path(...)` branch. The remaining body constructs `PostgresTicketRepository` unconditionally (the StorageConfig validator already guarantees `engine == "postgres"` and `dsn is not None`). Remove the now-unused `from foreman.v4.sqlite_repository import SqliteTicketRepository` import (line 32). Keep the `Path` import only if still used elsewhere in the file (`git grep -n "Path(" packages/foreman/src/foreman/v4/bootstrap.py`).

- [ ] **Step 6: Run config + bootstrap tests**

Run: `uv run pytest packages/foreman/tests/v4/test_config.py packages/foreman/tests/v4/test_bootstrap.py -o addopts="" -v`
Expected: the loud-fail test passes; bootstrap tests pass (they construct Postgres or inject a repo directly).

- [ ] **Step 7: Commit**

```bash
git add packages/foreman/src/foreman/v4/config.py packages/foreman/src/foreman/v4/bootstrap.py docker/foreman/config.toml.template packages/foreman/tests
git commit -m "feat(storage): Postgres-only selector with loud fail; drop SQLite db_path"
```

---

### Task 3: Remove the SQLite snapshot/backup subsystem

**Files:** delete `state_backup.py`, `cli/restore.py`, `tests/v4/test_state_backup.py`, `tests/v4/cli/test_restore.py`; update `daemon.py`, `bootstrap.py`, `config.py`, `events.py`, `cli/__init__.py`.

- [ ] **Step 1: Strip the backup scheduler from `daemon.py`**

Remove: the `from foreman.v4.state_backup import (BackupSchedulerLike, _DisabledBackupScheduler, ...)` import (`:33-45`); the `_DISABLED_BACKUP_SCHEDULER` module singleton (`:45`); the `backup_scheduler: BackupSchedulerLike = _DISABLED_BACKUP_SCHEDULER` constructor kwarg (`:94`); `self._backup_scheduler = backup_scheduler` (`:125`); and the `self._backup_scheduler.tick()` call in the tick loop (`:176`) plus its explanatory comment block. The daemon's tick loop loses one line; nothing else depends on it.

- [ ] **Step 2: Strip the backup wiring from `bootstrap.py`**

Remove the `from foreman.v4.state_backup import BackupScheduler, _DisabledBackupScheduler` import (`:33`) and the entire `backup_scheduler` construction block (`:160-185` region: the `if/else` choosing `_DisabledBackupScheduler()` vs `BackupScheduler.from_config(...)`). Remove the `backup_scheduler=backup_scheduler` argument from the `Daemon(...)` construction (`:213`).

- [ ] **Step 3: Remove `BackupConfig` from `config.py`**

Delete the `BackupConfig` class (`:257-281`) and the `backup: BackupConfig = Field(default_factory=BackupConfig)` field on `V4Config` (`:372`). Update any test config builder that sets `backup=...`.

- [ ] **Step 4: Remove the backup events**

Delete `BackupTakenEvent` (`events.py:117`) and `BackupFailedEvent` (`:132`), plus their entry in the module-docstring event tree (`:24-25`). Then `git grep -n "BackupTakenEvent\|BackupFailedEvent"` across `src/` — remove any observer `isinstance` branch or import that references them (e.g. in `observers/` or the structured-log observer). If an observer's only purpose was these events, delete it and its bootstrap subscription.

- [ ] **Step 5: Remove the `restore` command + delete the two src files**

In `cli/__init__.py`: delete the `from foreman.v4.cli.restore import cmd_restore` import (`:47`) and `app.command("restore")(cmd_restore)` (`:92`). Delete `packages/foreman/src/foreman/v4/state_backup.py` and `packages/foreman/src/foreman/v4/cli/restore.py`. Delete `packages/foreman/tests/v4/test_state_backup.py` and `packages/foreman/tests/v4/cli/test_restore.py`.

```bash
git rm packages/foreman/src/foreman/v4/state_backup.py packages/foreman/src/foreman/v4/cli/restore.py \
       packages/foreman/tests/v4/test_state_backup.py packages/foreman/tests/v4/cli/test_restore.py
```

- [ ] **Step 6: Verify no backup stragglers + run affected suites**

Run: `git grep -n "state_backup\|BackupConfig\|BackupScheduler\|BackupTakenEvent\|BackupFailedEvent\|backup_scheduler" -- packages/foreman/src`
Expected: zero hits.
Run: `uv run pytest packages/foreman/tests/v4/test_daemon.py packages/foreman/tests/v4/test_bootstrap.py packages/foreman/tests/v4/cli -o addopts="" -q`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add -A packages/foreman/src/foreman/v4 packages/foreman/tests/v4
git commit -m "refactor: remove SQLite snapshot/backup subsystem (state_backup + restore + events)"
```

---

### Task 4: Delete the SQLite repository impl + cutover tool

**Files:** delete `sqlite_repository.py`, `schema.sql`, `cli/migrate.py`, `tests/v4/test_sqlite_repository.py`, `tests/v4/test_migrate_v4_to_v5.py`; update `cli/__init__.py`, `_repository_contract.py`, `repository.py` docstring.

- [ ] **Step 1: Deregister the migrate command**

In `cli/__init__.py`: delete `from foreman.v4.cli.migrate import cmd_migrate_v4_to_v5` (`:34`) and `app.command("migrate-v4-to-v5")(cmd_migrate_v4_to_v5)` (`:94`).

- [ ] **Step 2: Delete the SQLite source + tests**

```bash
git rm packages/foreman/src/foreman/v4/sqlite_repository.py \
       packages/foreman/src/foreman/v4/schema.sql \
       packages/foreman/src/foreman/v4/cli/migrate.py \
       packages/foreman/tests/v4/test_sqlite_repository.py \
       packages/foreman/tests/v4/test_migrate_v4_to_v5.py
```

- [ ] **Step 3: Clean residual references**

`_repository_contract.py` had one `SqliteTicketRepository` reference (type hint/comment/binding) — remove or repoint it to `InMemoryTicketRepository`. Update `repository.py:191` docstring ("Behavior must match SqliteTicketRepository") → reference the Postgres impl / the shared contract instead. Then sweep:
Run: `git grep -n "sqlite\|SqliteTicketRepository\|schema.sql\|migrate_v4_to_v5\|\.at_path(\|\.in_memory(" -- packages/foreman/src`
Expected: zero hits in `src/` (a `schema.sql` reference only if Postgres has its own — confirm any remaining hit is Postgres DDL, not SQLite).
Run: `git grep -n "SqliteTicketRepository" -- packages/foreman/tests`
Expected: zero hits (all swapped in Task 1 or deleted here).

- [ ] **Step 4: Run the full v4 suite**

Run: `uv run pytest packages/foreman/tests/v4 -o addopts="" -q`
Expected: green. The contract suite now runs InMemory + Postgres only.

- [ ] **Step 5: Commit**

```bash
git add -A packages/foreman/src/foreman/v4 packages/foreman/tests/v4
git commit -m "refactor: delete SqliteTicketRepository, schema.sql, and the v4->v5 cutover tool"
```

---

### Task 5: Docs + import-linter + full gate

- [ ] **Step 1: Prune SQLite from the runbooks**

`git grep -ln "sqlite\|SQLite" -- docs`. In `docs/runbooks/postgres-migration.md` (and any sibling that documents SQLite as a live option), mark the SQLite→Postgres cutover as **historical/completed** and state SQLite is fully removed; drop instructions that present SQLite as a current backend. Keep the historical narrative if useful, but don't imply SQLite is selectable.

- [ ] **Step 2: Full gate**

Run: `just check`
Expected fully green: ruff + mypy + import-linter (R1/R2) + full pytest (incl. live Postgres) + coverage gate. Common post-deletion fixes: an unused import flagged by ruff, a stale type reference flagged by mypy, an import-linter contract naming a deleted module. Fix only kill-caused issues; stop and report any pre-existing/unrelated failure.

- [ ] **Step 3: Commit any gate fixes**

```bash
git add -A packages/foreman docs
git commit -m "docs+chore: prune SQLite references; green gate after SQLite removal"
```

---

### Task 6: Push + PR

- [ ] **Step 1:** Push the branch with the Wren PAT (GH_TOKEN env only; never echo/commit it). The pre-push hook runs `just check` — stay green, never `--no-verify`.
- [ ] **Step 2:** Open the PR (Wren PAT). Title: `refactor: remove SQLite backend entirely (Postgres-only)`. Body: summarize the three removed subsystems, the test-backend swap, the loud-fail selector, and link the spec + #434 (DR follow-up). Note the adversarial-review pass.
- [ ] **Step 3:** Surface the PR URL to Jeff. Do NOT merge — Wren reviews/merge is Jeff's call.

---

## Self-review checklist

1. **Spec coverage:** test backend → InMemory (Task 1) ✓; delete sqlite_repository + schema + migrate + backup subsystem (Tasks 3–4) ✓; Postgres-only loud-fail selector (Task 2) ✓; DR out of scope → #434 ✓; docs pruned (Task 5) ✓.
2. **Ordering safety:** tests swap to InMemory (Task 1) BEFORE the SQLite impl is deleted (Task 4) — the suite is runnable at every task boundary.
3. **No placeholders:** every file path + line ref is concrete; the one hedge is "the ~18 wiring test files" (Task 1 Step 1 enumerates them by grep) and the db_path/observer grep-confirmations (which guard against deleting a non-SQLite consumer).
4. **Loud fail is real:** Task 2 pins `engine` to `postgres` so the existing `_dsn_required_for_postgres` validator rejects an unset/dsn-less config — proven by the Step-1 test.

## Out of scope (tracked separately)

- Postgres DR backup (`pg_dump` cron / PITR) — jeffrichley/foreman#434.
- Stage 2 session resume — next spec/plan, built on the two-backend world.
