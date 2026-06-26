# Foreman — Kill SQLite — Design

**Date:** 2026-06-25 · **Author:** Wren (brainstormed with Jeff)
**Status:** design, pending review → implementation plan
**Follow-up tracked:** Postgres DR backup — jeffrichley/foreman#434

## Problem

Foreman v5 cut production storage over to Postgres (the `foreman-postgres`
`postgres:16-alpine` sidecar in `docker-compose.yml`; data on the
`foreman-pg-data` named volume; `FOREMAN_STORAGE_ENGINE=postgres`). SQLite is no
longer a production backend — but it lingers throughout the codebase:

- `SqliteTicketRepository` is still the **de-facto test backend** — 93 uses across
  22 test files (`SqliteTicketRepository.in_memory()`), even though an
  `InMemoryTicketRepository` already exists and already satisfies the shared
  repository contract.
- Two SQLite-specific subsystems ride along:
  - `migrate_v4_to_v5` (`cli/migrate.py`) — the **one-time SQLite→Postgres cutover
    tool**. Its job (porting prod data to Postgres) is already done.
  - The **SQLite snapshot/backup subsystem** — `state_backup.py` (`BackupScheduler`,
    `take_snapshot`, `prune_snapshots`), `cli/restore.py`, `BackupConfig`, the
    `BackupTakenEvent` / `BackupFailedEvent` bus events, and the bootstrap wiring.
    Its docstring (issue #360) calls itself the snapshotter of *"Foreman's
    authoritative SQLite state"* — but that state is **Postgres** now, so this
    scheduler either backs up a non-authoritative file (if enabled in prod) or is
    dead wiring (if disabled). Either way it protects **no real prod data**.

Keeping three repository backends in lockstep (InMemory / SQLite / Postgres) taxes
every storage change — including the imminent Stage-2 crash-recovery work, which
adds a `session_id` column. Writing that migration against a backend we're about to
delete is wasted effort. **Kill SQLite first; build Stage 2 on a two-backend world.**

## Goals / non-goals

**Goals.**
- Remove SQLite as a backend entirely: the impl, the schema, the cutover tool, and
  the snapshot/backup subsystem.
- Move the test suite onto `InMemoryTicketRepository` (fast double) with Postgres as
  the real-backend contract partner.
- Leave the codebase green (`just check`) and production behavior unchanged (prod is
  already Postgres).

**Non-goals.**
- The replacement **Postgres DR backup** — tracked separately as #434. This spec
  only *removes* the dead SQLite backup; it does not build the Postgres one.
- Stage 2 (session resume) — its own spec/plan, built on top of this.
- Any change to the Postgres impl's behavior.

## Decisions

### 1. Test backend → `InMemoryTicketRepository`

`InMemoryTicketRepository` (`repository.py:188`) already exists and already passes
the shared `_repository_contract.py`. Every test currently using
`SqliteTicketRepository.in_memory()` switches to `InMemoryTicketRepository`. The
shared contract suite parametrizes **InMemory + Postgres** (SQLite param dropped).
Live-Postgres integration tests are unchanged.

This is the conventional fast-double + real-backend pattern. SQLite-*specific* tests
(WAL mode, file persistence in `test_sqlite_repository.py`) are deleted outright —
they test a backend that no longer exists.

### 2. SQLite-specific features → delete both

- **`migrate_v4_to_v5`** — spent cutover tool. Delete `cli/migrate.py`,
  `test_migrate_v4_to_v5.py`, and its CLI registration.
- **SQLite snapshot/backup subsystem** — delete `state_backup.py`, `cli/restore.py`,
  `BackupConfig`, the two backup bus events, their observer handling, the bootstrap
  wiring (`bootstrap.py:33,164–184`), and their tests (`test_state_backup.py`,
  `test_restore.py`). Removing this retires **no real DR coverage** (it never touched
  Postgres). The replacement Postgres DR path is #434.

### 3. Storage-engine selection → Postgres-only in production

`FOREMAN_STORAGE_ENGINE` currently defaults to `sqlite` when unset (per the compose
comment) and selects the SQLite impl. After the kill:
- Production selectable engine is **Postgres only**. The SQLite branch in the
  bootstrap/entrypoint storage selector is removed.
- The unset/invalid case becomes a **loud failure** (require Postgres DSN) rather than
  a silent SQLite fallback — no accidental SQLite resurrection.
- `InMemoryTicketRepository` remains test-only (injected via `build_cli_context`, not
  env-selectable), exactly as today.

## Scope (file inventory)

**Delete (src):** `sqlite_repository.py`, `state_backup.py`, `schema.sql` (SQLite
DDL), `cli/migrate.py`, `cli/restore.py`.

**Delete (tests):** `test_sqlite_repository.py`, `test_state_backup.py`,
`test_migrate_v4_to_v5.py`, `cli/test_restore.py`.

**Update (src):**
- `bootstrap.py` — drop the `BackupScheduler` import + wiring; drop the SQLite
  storage-engine branch.
- `config.py` — drop `BackupConfig` (+ its `V5Config`/`V4Config` field).
- `events.py` — drop `BackupTakenEvent` / `BackupFailedEvent`; drop any observer that
  subscribes to them.
- `repository.py` — module docstring no longer references SQLite.
- `cli/__init__.py` — deregister `migrate` + `restore` commands.
- the storage-selection entrypoint — Postgres-only + loud-fail-on-unset.

**Update (tests):** the ~22 files swapping `SqliteTicketRepository` →
`InMemoryTicketRepository`; `_repository_contract.py` (drop SQLite param).

**Docs:** prune SQLite references in the storage/migration runbooks
(`docs/runbooks/postgres-migration.md` and friends) — note SQLite is fully removed
and the cutover is historical.

## Risks / mitigations

- **InMemory ≠ SQLite behavioral drift.** Mitigation: the shared contract suite is the
  guarantee the two impls behave identically; InMemory already passes it. Any test that
  was secretly relying on SQL-specific semantics (rather than the contract) surfaces as
  a failure during the swap and is fixed then.
- **Hidden SQLite coupling beyond the inventory.** Mitigation: a grep sweep for
  `sqlite`, `SqliteTicketRepository`, `state_backup`, `BackupConfig`, `migrate_v4_to_v5`,
  `restore` across `src/` + `tests/` before final gate; import-linter + mypy catch
  dangling references.
- **Storage-selector change breaks a non-compose caller.** Mitigation: prod uses the
  compose stack (`FOREMAN_STORAGE_ENGINE=postgres` explicit); the loud-fail only bites a
  misconfigured invocation, which is the desired signal, not a regression.

## Testing

- Full `just check` green after the swap: ruff + mypy + import-linter + pytest (incl.
  live Postgres) + coverage gate.
- The repository contract suite runs **InMemory + Postgres** and stays green.
- A grep sweep confirms zero remaining `sqlite` / `SqliteTicketRepository` /
  `state_backup` / `BackupConfig` references in `src/` (tests may keep an
  `InMemoryTicketRepository` import only).

## Out of scope (tracked separately)

- Postgres DR backup (`pg_dump` cron / PITR) — #434.
- Stage 2 session resume — next spec/plan, built on the two-backend world.
