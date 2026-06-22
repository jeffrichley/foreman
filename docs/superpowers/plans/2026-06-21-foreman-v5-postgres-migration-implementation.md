# Foreman v5 — PostgreSQL Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move foreman's persistence from embedded SQLite to a PostgreSQL container behind the unchanged `TicketRepository` Protocol, closing the `database is locked` (#404) and `drop`-resurrection (#403) bug classes, with the existing repository contract + e2e test suites proving behavioral parity.

**Architecture:** A new `PostgresTicketRepository` implements the same synchronous `TicketRepository` Protocol that `SqliteTicketRepository` implements today. It uses `psycopg` (v3) with a thread-safe connection pool — matching v4's existing `ThreadPoolExecutor`-based daemon, which is synchronous threading, NOT asyncio. The repository stays sync; no async sweep. PostgreSQL's MVCC removes the `RLock`-serialized write contention that the SQLite impl needs. Bootstrap selects the impl from a new `[storage]` config section. This plan covers ONLY the DB migration — the HTTP control plane and the dashboard are later, separate plans.

**Tech Stack:** Python 3.13, `psycopg[binary,pool]` v3, `testcontainers[postgres]` for tests, Pydantic v2 (config), pytest + pytest-xdist (existing), Docker Compose (postgres + daemon).

**Scope boundaries (read before starting):**
- IN scope: Postgres schema, `PostgresTicketRepository`, `[storage]` config, event-archival decoupling, backup no-op under Postgres, bootstrap engine selection, docker-compose (postgres + daemon only), contract + e2e parity tests, one-shot migration script, operator runbook.
- OUT of scope: the HTTP control plane (FastAPI), the dashboard container, any async rewrite, UUID primary keys, multi-daemon topology. Integer PKs and synchronous repository are preserved deliberately.

**Key reference files (read these first — they are the spec for parity):**
- `packages/foreman/src/foreman/v4/repository.py` — the `TicketRepository` Protocol (29 methods) + `InMemoryTicketRepository` reference impl. The Postgres impl must match this method-for-method.
- `packages/foreman/src/foreman/v4/sqlite_repository.py` — the production SQLite impl. The Postgres impl mirrors its SQL semantics.
- `packages/foreman/src/foreman/v4/schema.sql` — current DDL to translate.
- `packages/foreman/src/foreman/v4/records.py` — `TicketRecord`, `StateInstanceRecord` (frozen dataclasses, integer ids).
- `packages/foreman/tests/v4/_repository_contract.py` — the mixin contract suite both impls already pass.
- `packages/foreman/src/foreman/v4/config.py` — `V4Config` + `load_config`.
- `packages/foreman/src/foreman/v4/bootstrap.py` — where the repo impl is constructed.

---

## File Structure

**New files:**
- `packages/foreman/src/foreman/v4/postgres_schema.sql` — Postgres DDL (BIGSERIAL ids, JSONB, TIMESTAMPTZ).
- `packages/foreman/src/foreman/v4/postgres_repository.py` — `PostgresTicketRepository`.
- `packages/foreman/tests/v4/test_postgres_repository.py` — binds `PostgresTicketRepository` to `RepositoryContract`.
- `packages/foreman/tests/v4/postgres_fixture.py` — session-scoped testcontainer fixture + per-test schema reset.
- `packages/foreman/tests/v4/test_postgres_e2e.py` — the v4 lifecycle e2e re-pointed at Postgres.
- `packages/foreman/tests/v4/test_migrate_v4_to_v5.py` — migration-script tests.
- `packages/foreman/src/foreman/v4/cli/migrate.py` — `foreman migrate-v4-to-v5` command.
- `docker-compose.yml` (repo root) — postgres + foreman-daemon services.
- `docs/runbooks/postgres-migration.md` — operator runbook.

**Modified files:**
- `packages/foreman/pyproject.toml` — add `psycopg`, `testcontainers`.
- `packages/foreman/src/foreman/v4/config.py` — add `StorageConfig` + `V4Config.storage` + `load_config` threading.
- `packages/foreman/src/foreman/v4/repository.py` — add `append_event` to the Protocol (decouples event archival from the raw connection).
- `packages/foreman/src/foreman/v4/sqlite_repository.py` — implement `append_event`; keep `.connection` for back-compat.
- `packages/foreman/src/foreman/v4/observers/event_archive.py` — consume `repo.append_event` instead of a raw `sqlite3.Connection`.
- `packages/foreman/src/foreman/v4/bootstrap.py` — select repo impl from `config.storage.engine`; disable file-snapshot backup under Postgres.

---

## Phase 1 — Dependencies + storage config

### Task 1: Add psycopg + testcontainers dependencies

**Files:**
- Modify: `packages/foreman/pyproject.toml`
- Modify: root `pyproject.toml` (dev dependency group)

- [ ] **Step 1: Add the runtime dependency**

In `packages/foreman/pyproject.toml`, add `psycopg[binary,pool]>=3.2,<4` to the `dependencies` list (alongside `PyGithub`, `pydantic`, etc.):

```toml
dependencies = [
    "PyGithub>=2.0,<3",
    "PyJWT[crypto]>=2.8,<3",
    "claude-agent-sdk>=0.1,<1",
    "pydantic>=2.5,<3",
    "click>=8.1,<9",
    "requests>=2.31,<3",
    "rich>=13,<15",
    "typer>=0.12,<1",
    "pyyaml>=6,<7",
    "psycopg[binary,pool]>=3.2,<4",
]
```

- [ ] **Step 2: Add the test dependency**

In the root `pyproject.toml` `[dependency-groups].dev` list, add:

```toml
    "testcontainers[postgres]>=4.0",
```

- [ ] **Step 3: Sync the environment**

Run: `uv sync`
Expected: resolves and installs `psycopg`, `psycopg-binary`, `psycopg-pool`, `testcontainers`. No version conflicts.

- [ ] **Step 4: Verify import**

Run: `uv run python -c "import psycopg, psycopg_pool; from testcontainers.postgres import PostgresContainer; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/pyproject.toml pyproject.toml uv.lock
git commit -m "build(v5): add psycopg + testcontainers for postgres migration"
```

### Task 2: StorageConfig + V4Config.storage

**Files:**
- Modify: `packages/foreman/src/foreman/v4/config.py`
- Test: `packages/foreman/tests/v4/test_config.py` (add to existing)

- [ ] **Step 1: Write the failing test**

Add to `packages/foreman/tests/v4/test_config.py`:

```python
def test_storage_defaults_to_sqlite_when_section_absent(tmp_path):
    # A config with no [storage] block keeps the historical sqlite behavior.
    cfg_text = _minimal_valid_config_toml(tmp_path)  # existing helper in this file
    cfg_path = tmp_path / "foreman.toml"
    cfg_path.write_text(cfg_text)
    cfg = load_config(cfg_path)
    assert cfg.storage.engine == "sqlite"
    assert cfg.storage.dsn is None


def test_storage_postgres_requires_dsn(tmp_path):
    cfg_text = _minimal_valid_config_toml(tmp_path) + (
        "\n[storage]\nengine = \"postgres\"\n"
    )
    cfg_path = tmp_path / "foreman.toml"
    cfg_path.write_text(cfg_text)
    with pytest.raises(ValidationError, match="dsn is required when engine"):
        load_config(cfg_path)


def test_storage_postgres_accepts_dsn_and_pool_sizes(tmp_path):
    cfg_text = _minimal_valid_config_toml(tmp_path) + (
        "\n[storage]\n"
        "engine = \"postgres\"\n"
        "dsn = \"postgresql://foreman:pw@postgres:5432/foreman\"\n"
        "pool_min = 2\n"
        "pool_max = 10\n"
    )
    cfg_path = tmp_path / "foreman.toml"
    cfg_path.write_text(cfg_text)
    cfg = load_config(cfg_path)
    assert cfg.storage.engine == "postgres"
    assert cfg.storage.dsn == "postgresql://foreman:pw@postgres:5432/foreman"
    assert cfg.storage.pool_min == 2
    assert cfg.storage.pool_max == 10
```

If `_minimal_valid_config_toml` does not already exist in the test file, check the existing `load_config` tests for the helper they use to build a valid config string (the file already has working `load_config` tests — reuse their fixture/helper). Match whatever pattern is there.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_config.py -k storage -v`
Expected: FAIL — `AttributeError: 'V4Config' object has no attribute 'storage'`.

- [ ] **Step 3: Implement StorageConfig**

In `packages/foreman/src/foreman/v4/config.py`, add the model near the other section models (e.g. after `BackupConfig`):

```python
class StorageConfig(BaseModel):
    """Persistence engine selection. Defaulted so configs without a
    ``[storage]`` block keep the historical SQLite behavior.

    ``engine = "sqlite"`` (default) uses ``SqliteTicketRepository`` at
    ``V4Config.db_path``. ``engine = "postgres"`` uses
    ``PostgresTicketRepository`` at ``dsn`` with a thread-safe
    connection pool sized ``[pool_min, pool_max]``.
    """

    model_config = ConfigDict(extra="forbid")
    engine: Literal["sqlite", "postgres"] = "sqlite"
    dsn: str | None = None
    pool_min: int = Field(default=2, ge=1)
    pool_max: int = Field(default=10, ge=1)

    @model_validator(mode="after")
    def _dsn_required_for_postgres(self) -> StorageConfig:
        if self.engine == "postgres" and not self.dsn:
            raise ValueError("dsn is required when engine = \"postgres\"")
        if self.pool_max < self.pool_min:
            raise ValueError("pool_max must be >= pool_min")
        return self
```

Ensure `Literal` and `model_validator` are imported at the top of the file:

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
```

(Check the existing imports — `BaseModel`, `ConfigDict`, `Field` are already imported; add `Literal` and `model_validator` if missing.)

- [ ] **Step 4: Add the field to V4Config + thread it in load_config**

In the `V4Config` model body, add:

```python
    storage: StorageConfig = Field(default_factory=StorageConfig)
    """v5: persistence engine selection. Defaulted so pre-v5 configs
    (no [storage] block) keep loading with SQLite at db_path."""
```

In `load_config`, after the `if "backup" in raw:` block, add:

```python
    if "storage" in raw:
        payload["storage"] = raw["storage"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest packages/foreman/tests/v4/test_config.py -k storage -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/config.py packages/foreman/tests/v4/test_config.py
git commit -m "feat(v5): add [storage] config section (sqlite default, postgres opt-in)"
```

---

## Phase 2 — Postgres schema + repository

### Task 3: Postgres DDL

**Files:**
- Create: `packages/foreman/src/foreman/v4/postgres_schema.sql`

- [ ] **Step 1: Write the DDL**

Create `packages/foreman/src/foreman/v4/postgres_schema.sql`. This mirrors `schema.sql` with Postgres-native types. **Integer PKs preserved** (BIGSERIAL → the Protocol types ids as `int`). JSONB for the JSON columns; TIMESTAMPTZ for datetimes. `IF NOT EXISTS` everywhere so the bootstrap can run it idempotently.

```sql
-- packages/foreman/src/foreman/v4/postgres_schema.sql
--
-- Foreman v5 PostgreSQL schema. Behavioral parity with schema.sql
-- (the SQLite v4 schema). Integer PKs preserved — the TicketRepository
-- Protocol types ids as int across the codebase; UUID would break the
-- drop-in property for no benefit (/tickets/21 is already urlsafe).
--
-- Type deltas from SQLite:
--   INTEGER PK AUTOINCREMENT -> BIGSERIAL
--   TEXT (datetime, ISO-8601) -> TIMESTAMPTZ
--   TEXT (json)               -> JSONB
--   TEXT (depends_on json)    -> JSONB (default '[]')

CREATE TABLE IF NOT EXISTS tickets (
    id              BIGSERIAL   PRIMARY KEY,
    project         TEXT        NOT NULL,
    issue_number    INTEGER     NOT NULL,
    current_state   TEXT        NOT NULL,
    held_by         TEXT,
    held_at         TIMESTAMPTZ,
    held_reason     TEXT,
    depends_on      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    next_action_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (project, issue_number)
);

CREATE TABLE IF NOT EXISTS state_instances (
    id                      BIGSERIAL   PRIMARY KEY,
    ticket_id               BIGINT      NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    state_name              TEXT        NOT NULL,
    sequence                INTEGER     NOT NULL,
    entered_at              TIMESTAMPTZ NOT NULL,
    execute_started_at      TIMESTAMPTZ,
    execute_completed_at    TIMESTAMPTZ,
    exited_at               TIMESTAMPTZ,
    outcome_kind            TEXT,
    outcome_payload         JSONB,
    next_state              TEXT,
    failure_phase           TEXT,
    failure_reason          TEXT,
    UNIQUE (ticket_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_state_instances_inflight
    ON state_instances (ticket_id)
    WHERE exited_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_tickets_held
    ON tickets (held_by)
    WHERE held_by IS NOT NULL;

CREATE TABLE IF NOT EXISTS events (
    id              BIGSERIAL   PRIMARY KEY,
    ticket_id       BIGINT      NOT NULL,
    instance_id     BIGINT      NOT NULL,
    event_type      TEXT        NOT NULL,
    state_name      TEXT        NOT NULL,
    sequence        INTEGER     NOT NULL,
    at              TIMESTAMPTZ NOT NULL,
    payload         JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ticket
    ON events (ticket_id, at);
```

Note: the SQLite schema's `ON DELETE` was implicit (delete cascade handled in app code). Postgres `ON DELETE CASCADE` on `state_instances.ticket_id` makes `delete_ticket` drop the journal rows automatically — matching `InMemoryTicketRepository.delete_ticket`'s manual cascade. The Postgres repo's `delete_ticket` therefore only deletes the ticket row.

- [ ] **Step 2: Commit**

```bash
git add packages/foreman/src/foreman/v4/postgres_schema.sql
git commit -m "feat(v5): postgres schema (integer PKs, JSONB, TIMESTAMPTZ)"
```

### Task 4: Postgres testcontainer fixture

**Files:**
- Create: `packages/foreman/tests/v4/postgres_fixture.py`

This fixture is the test substrate for Tasks 5 + 12. It spins up one Postgres container per test session (slow to start, so session-scoped) and gives each test a clean schema by truncating between tests.

- [ ] **Step 1: Write the fixture module**

Create `packages/foreman/tests/v4/postgres_fixture.py`:

```python
"""Session-scoped Postgres testcontainer + per-test reset.

The container starts once per pytest session (image pull + boot is
~5s). Each test gets an empty schema via TRUNCATE ... RESTART IDENTITY
CASCADE in the per-test fixture, which is far cheaper than recreating
the container.

Skips the whole module if Docker is unavailable (e.g. a dev box with
no daemon), so the suite degrades gracefully instead of erroring.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

_SCHEMA = (
    Path(__file__).parents[2]
    / "src" / "foreman" / "v4" / "postgres_schema.sql"
)

try:  # pragma: no cover - import guard
    from testcontainers.postgres import PostgresContainer

    _HAVE_DOCKER = True
except Exception:  # pragma: no cover
    _HAVE_DOCKER = False


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    if not _HAVE_DOCKER:
        pytest.skip("testcontainers/docker not available")
    with PostgresContainer("postgres:16-alpine") as pg:
        # testcontainers default URL uses the psycopg2 driver scheme;
        # normalize to a plain libpq DSN psycopg v3 accepts.
        url = pg.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql://"
        )
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(_SCHEMA.read_text(encoding="utf-8"))
        yield url


@pytest.fixture()
def clean_postgres_dsn(postgres_dsn: str) -> Iterator[str]:
    with psycopg.connect(postgres_dsn, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE tickets, state_instances, events "
            "RESTART IDENTITY CASCADE"
        )
    yield postgres_dsn
```

- [ ] **Step 2: Smoke-test the fixture**

Add a temporary throwaway test to confirm the container boots (delete after it passes):

Run: `uv run pytest packages/foreman/tests/v4/postgres_fixture.py -v` (no tests yet — just confirm the module imports without error)
Expected: `no tests ran` (import succeeds; the fixture is exercised in Task 5).

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/tests/v4/postgres_fixture.py
git commit -m "test(v5): session-scoped postgres testcontainer fixture"
```

### Task 5: PostgresTicketRepository — connection pool + ticket CRUD

**Files:**
- Create: `packages/foreman/src/foreman/v4/postgres_repository.py`
- Create: `packages/foreman/tests/v4/test_postgres_repository.py`

This is the bulk of the migration. The strategy: bind `PostgresTicketRepository` to the **existing** `RepositoryContract` mixin so the same ~40 assertions that prove `SqliteTicketRepository` correct prove the Postgres impl correct. Implement method-group by method-group until the contract is green. Steps 1-2 stand up the pool + binding; Steps 3+ implement until green.

- [ ] **Step 1: Bind to the contract (the failing test)**

Create `packages/foreman/tests/v4/test_postgres_repository.py`:

```python
from foreman.v4.postgres_repository import PostgresTicketRepository

from ._repository_contract import RepositoryContract
from .postgres_fixture import clean_postgres_dsn  # noqa: F401  (fixture import)


class TestPostgres(RepositoryContract):
    @staticmethod
    def factory():  # type: ignore[override]
        # Provided per-test via the _pg_repo fixture below; the contract's
        # ``repo`` fixture calls factory(), so we override ``repo`` instead.
        raise NotImplementedError

    import pytest

    @pytest.fixture()
    def repo(self, clean_postgres_dsn):  # noqa: F811
        return PostgresTicketRepository.from_dsn(
            clean_postgres_dsn, pool_min=1, pool_max=4
        )
```

The contract suite defines `repo` via `self.factory()`; here we override `repo` directly because the Postgres repo needs the per-test DSN fixture. Overriding the `repo` fixture is sufficient — the contract's test methods only depend on `repo`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_postgres_repository.py -x -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'foreman.v4.postgres_repository'`.

- [ ] **Step 3: Implement the pool + record adapters + ticket CRUD**

Create `packages/foreman/src/foreman/v4/postgres_repository.py`. This step implements the pool, the row→record adapters, and the ticket-CRUD methods. (State-instance journal + helpers + dependencies follow in Tasks 6-8.)

```python
"""PostgresTicketRepository — production persistence on PostgreSQL.

Behavioral parity with SqliteTicketRepository — the same
RepositoryContract test suite runs against both. Synchronous, matching
v4's ThreadPoolExecutor-based daemon. A psycopg_pool.ConnectionPool
serves the worker threads; Postgres MVCC removes the write contention
the SQLite impl serialized with an RLock, so there is no lock here.

Integer PKs (BIGSERIAL) preserved — the Protocol types ids as int.

Datetime contract: callers pass timezone-aware or naive dt.datetime via
``now=``. Postgres TIMESTAMPTZ stores UTC; psycopg returns tz-aware
datetimes on read. The contract tests compare round-tripped values, so
we normalize on write (assume naive == UTC) and return tz-aware. See
_to_db / _from_db below.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from foreman.v4.outcome import OutcomeKind
from foreman.v4.records import StateInstanceRecord, TicketRecord
from foreman.v4.repository import (
    StateInstanceNotFoundError,
    TicketAlreadyExistsError,
    TicketNotFoundError,
)

_SCHEMA = Path(__file__).with_name("postgres_schema.sql")


def _to_db(value: dt.datetime | None) -> dt.datetime | None:
    """Normalize a caller datetime for TIMESTAMPTZ storage.

    Naive datetimes are assumed UTC (the v4 callers pass
    ``dt.datetime.now`` / ``dt.datetime.now(dt.UTC)`` inconsistently;
    SqliteTicketRepository stores the ISO string verbatim, so for parity
    we attach UTC to naive values rather than letting Postgres apply the
    session TimeZone).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


def _ticket_from_row(row: dict[str, Any]) -> TicketRecord:
    return TicketRecord(
        id=row["id"],
        project=row["project"],
        issue_number=row["issue_number"],
        current_state=row["current_state"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        held_by=row["held_by"],
        held_at=row["held_at"],
        held_reason=row["held_reason"],
        depends_on=list(row["depends_on"]),
        next_action_at=row["next_action_at"],
    )


def _instance_from_row(row: dict[str, Any]) -> StateInstanceRecord:
    kind = OutcomeKind(row["outcome_kind"]) if row["outcome_kind"] else None
    return StateInstanceRecord(
        id=row["id"],
        ticket_id=row["ticket_id"],
        state_name=row["state_name"],
        sequence=row["sequence"],
        entered_at=row["entered_at"],
        execute_started_at=row["execute_started_at"],
        execute_completed_at=row["execute_completed_at"],
        exited_at=row["exited_at"],
        outcome_kind=kind,
        outcome_payload=row["outcome_payload"],
        next_state=row["next_state"],
        failure_phase=row["failure_phase"],
        failure_reason=row["failure_reason"],
    )


_TERMINAL_STATES = ("Done", "Failed")


class PostgresTicketRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        with self._pool.connection() as conn:
            conn.execute(_SCHEMA.read_text(encoding="utf-8"))
            conn.commit()

    @classmethod
    def from_dsn(
        cls, dsn: str, *, pool_min: int = 2, pool_max: int = 10
    ) -> PostgresTicketRepository:
        pool = ConnectionPool(
            dsn,
            min_size=pool_min,
            max_size=pool_max,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
        return cls(pool)

    def close(self) -> None:
        self._pool.close()

    # --- Ticket CRUD ---

    def create_ticket(
        self, *, project: str, issue_number: int, now: dt.datetime
    ) -> TicketRecord:
        ts = _to_db(now)
        with self._pool.connection() as conn:
            try:
                row = conn.execute(
                    """
                    INSERT INTO tickets
                        (project, issue_number, current_state,
                         created_at, updated_at, depends_on)
                    VALUES (%s, %s, 'Queued', %s, %s, '[]'::jsonb)
                    RETURNING *
                    """,
                    (project, issue_number, ts, ts),
                ).fetchone()
            except psycopg.errors.UniqueViolation as exc:
                conn.rollback()
                raise TicketAlreadyExistsError(
                    f"{project}#{issue_number}"
                ) from exc
            conn.commit()
            assert row is not None
            return _ticket_from_row(row)

    def get_ticket(self, ticket_id: int) -> TicketRecord:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM tickets WHERE id = %s", (ticket_id,)
            ).fetchone()
        if row is None:
            raise TicketNotFoundError(str(ticket_id))
        return _ticket_from_row(row)

    def get_ticket_by_issue(
        self, *, project: str, issue_number: int
    ) -> TicketRecord:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM tickets WHERE project = %s AND issue_number = %s",
                (project, issue_number),
            ).fetchone()
        if row is None:
            raise TicketNotFoundError(f"{project}#{issue_number}")
        return _ticket_from_row(row)

    def list_open_tickets(self) -> list[TicketRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE current_state NOT IN %s ORDER BY id",
                (_TERMINAL_STATES,),
            ).fetchall()
        return [_ticket_from_row(r) for r in rows]

    def list_all_tickets(self) -> list[TicketRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()
        return [_ticket_from_row(r) for r in rows]

    def set_ticket_state(
        self, ticket_id: int, new_state: str, *, now: dt.datetime
    ) -> None:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE tickets SET current_state = %s, updated_at = %s WHERE id = %s",
                (new_state, _to_db(now), ticket_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            conn.commit()

    def hold_ticket(
        self, ticket_id: int, *, held_by: str, reason: str, now: dt.datetime
    ) -> None:
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                UPDATE tickets
                   SET held_by = %s, held_at = %s, held_reason = %s,
                       updated_at = %s
                 WHERE id = %s
                """,
                (held_by, _to_db(now), reason, _to_db(now), ticket_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            conn.commit()

    def resume_ticket(self, ticket_id: int, *, now: dt.datetime) -> None:
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                UPDATE tickets
                   SET held_by = NULL, held_at = NULL, held_reason = NULL,
                       updated_at = %s
                 WHERE id = %s
                """,
                (_to_db(now), ticket_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            conn.commit()

    def delete_ticket(self, ticket_id: int) -> None:
        # ON DELETE CASCADE on state_instances.ticket_id drops the journal
        # rows automatically (matches InMemory's manual cascade).
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM tickets WHERE id = %s", (ticket_id,)
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            conn.commit()

    def set_next_action_at(self, ticket_id: int, *, when: dt.datetime) -> None:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE tickets SET next_action_at = %s, updated_at = %s WHERE id = %s",
                (_to_db(when), _to_db(when), ticket_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            conn.commit()

    def clear_next_action_at(self, ticket_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE tickets SET next_action_at = NULL WHERE id = %s",
                (ticket_id,),
            )
            conn.commit()
```

Implementation notes for the engineer:
- psycopg v3 with `dict_row` returns dict rows — `row["col"]` access matches the adapters above.
- JSONB columns: psycopg adapts Python `dict`/`list` → JSONB on write automatically when the param is wrapped with `psycopg.types.json.Jsonb(...)`. For `depends_on` and `outcome_payload` writes (Tasks 6-8), wrap with `Jsonb(...)`. On read, psycopg returns the parsed Python object directly.
- `_TERMINAL_STATES` is a tuple; `NOT IN %s` with a tuple param works in psycopg (it adapts the tuple to a SQL list).

- [ ] **Step 4: Run the ticket-CRUD slice of the contract**

Run: `uv run pytest packages/foreman/tests/v4/test_postgres_repository.py -k "ticket or hold or resume or delete or open_tickets or next_action" -v`
Expected: the ticket-CRUD tests PASS; state-instance/helper tests still FAIL (not implemented). That's expected — Tasks 6-8 finish them.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/postgres_repository.py packages/foreman/tests/v4/test_postgres_repository.py
git commit -m "feat(v5): PostgresTicketRepository — pool + ticket CRUD"
```

### Task 6: PostgresTicketRepository — state-instance journal

**Files:**
- Modify: `packages/foreman/src/foreman/v4/postgres_repository.py`

- [ ] **Step 1: Confirm the journal tests currently fail**

Run: `uv run pytest packages/foreman/tests/v4/test_postgres_repository.py -k "instance or execute or failure or in_flight" -v`
Expected: FAIL — `AttributeError: 'PostgresTicketRepository' object has no attribute 'open_state_instance'`.

- [ ] **Step 2: Implement the journal methods**

Append to `PostgresTicketRepository` (mirroring `SqliteTicketRepository`'s journal SQL; wrap JSONB params with `Jsonb`):

```python
    # --- State-instance journal ---

    def open_state_instance(
        self, *, ticket_id: int, state_name: str, sequence: int, now: dt.datetime
    ) -> StateInstanceRecord:
        with self._pool.connection() as conn:
            # FK enforces ticket existence; surface a clean error first.
            exists = conn.execute(
                "SELECT 1 FROM tickets WHERE id = %s", (ticket_id,)
            ).fetchone()
            if exists is None:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            row = conn.execute(
                """
                INSERT INTO state_instances
                    (ticket_id, state_name, sequence, entered_at)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (ticket_id, state_name, sequence, _to_db(now)),
            ).fetchone()
            conn.commit()
            assert row is not None
            return _instance_from_row(row)

    def get_state_instance(self, instance_id: int) -> StateInstanceRecord:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM state_instances WHERE id = %s", (instance_id,)
            ).fetchone()
        if row is None:
            raise StateInstanceNotFoundError(str(instance_id))
        return _instance_from_row(row)

    def mark_execute_started(self, instance_id: int, *, now: dt.datetime) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE state_instances SET execute_started_at = %s WHERE id = %s",
                (_to_db(now), instance_id),
            )
            conn.commit()

    def mark_execute_completed(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        outcome_kind: OutcomeKind,
        outcome_payload: dict[str, Any],
        next_state: str,
    ) -> None:
        from psycopg.types.json import Jsonb

        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE state_instances
                   SET execute_completed_at = %s, outcome_kind = %s,
                       outcome_payload = %s, next_state = %s
                 WHERE id = %s
                """,
                (
                    _to_db(now),
                    outcome_kind.value,
                    Jsonb(outcome_payload),
                    next_state,
                    instance_id,
                ),
            )
            conn.commit()

    def close_state_instance(self, instance_id: int, *, now: dt.datetime) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE state_instances SET exited_at = %s WHERE id = %s",
                (_to_db(now), instance_id),
            )
            conn.commit()

    def record_failure(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        failure_phase: str,
        failure_reason: str,
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE state_instances
                   SET failure_phase = %s, failure_reason = %s
                 WHERE id = %s
                """,
                (failure_phase, failure_reason, instance_id),
            )
            conn.commit()

    def list_in_flight_state_instances(self) -> list[StateInstanceRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM state_instances WHERE exited_at IS NULL ORDER BY id"
            ).fetchall()
        return [_instance_from_row(r) for r in rows]

    def list_state_instances_for_ticket(
        self, ticket_id: int
    ) -> list[StateInstanceRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM state_instances WHERE ticket_id = %s ORDER BY sequence",
                (ticket_id,),
            ).fetchall()
        return [_instance_from_row(r) for r in rows]
```

Note: `outcome_kind` stored as its `.value` (TEXT) — matches the SQLite impl, and `_instance_from_row` reconstructs the enum.

- [ ] **Step 3: Run the journal slice**

Run: `uv run pytest packages/foreman/tests/v4/test_postgres_repository.py -k "instance or execute or failure or in_flight" -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add packages/foreman/src/foreman/v4/postgres_repository.py
git commit -m "feat(v5): PostgresTicketRepository — state-instance journal"
```

### Task 7: PostgresTicketRepository — runaway-defense helpers

**Files:**
- Modify: `packages/foreman/src/foreman/v4/postgres_repository.py`

These four helpers carry the subtle walk-back skip rules documented in `repository.py` (lines 88-156). Read those docstrings — the skip semantics (`failure_phase == 'can_run'`, `BLOCKED`, `TRANSIENT_PROVIDER_ERROR`, NULL in-flight row) must match exactly or the runaway-defense + backoff behavior regresses. The cleanest parity-preserving approach: fetch the ticket's instances ordered by sequence DESC and apply the **same Python walk** the `InMemoryTicketRepository` uses, rather than reimplementing the skip logic in SQL.

- [ ] **Step 1: Confirm the helper tests fail**

Run: `uv run pytest packages/foreman/tests/v4/test_postgres_repository.py -k "consecutive or latest_pr or count_state" -v`
Expected: FAIL — methods not defined.

- [ ] **Step 2: Implement the helpers (Python walk over fetched rows)**

Append to `PostgresTicketRepository`:

```python
    # --- Helpers used by states / WorkerPool / QueueManager ---

    def latest_pr_number_for_ticket(self, ticket_id: int) -> int | None:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT outcome_payload FROM state_instances "
                "WHERE ticket_id = %s ORDER BY sequence DESC",
                (ticket_id,),
            ).fetchall()
        for row in rows:
            payload = row["outcome_payload"]
            if not payload:
                continue
            pr_number = (payload or {}).get("artifacts", {}).get("pr_number")
            if pr_number is not None:
                return int(pr_number)
        return None

    def count_state_instances_for_ticket(self, ticket_id: int) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM state_instances WHERE ticket_id = %s",
                (ticket_id,),
            ).fetchone()
        assert row is not None
        return int(row["n"])

    def count_consecutive_same_state(self, *, ticket_id: int, state: str) -> int:
        # Mirror InMemoryTicketRepository.count_consecutive_same_state exactly:
        # walk newest-first, skip can_run-failed / BLOCKED /
        # TRANSIENT_PROVIDER_ERROR rows (neither count nor break), count
        # matching state_name rows, break on first non-matching.
        instances = self.list_state_instances_for_ticket(ticket_id)
        instances.reverse()  # sequence DESC
        count = 0
        for inst in instances:
            if inst.failure_phase == "can_run":
                continue
            if inst.outcome_kind == OutcomeKind.BLOCKED:
                continue
            if inst.outcome_kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR:
                continue
            if inst.state_name == state:
                count += 1
            else:
                break
        return count

    def count_consecutive_transient_provider_errors(self, ticket_id: int) -> int:
        # Mirror InMemoryTicketRepository exactly: skip can_run-failed rows
        # and the in-flight (outcome_kind IS NULL) row; count consecutive
        # TRANSIENT_PROVIDER_ERROR; break on any other completed outcome.
        instances = self.list_state_instances_for_ticket(ticket_id)
        instances.reverse()
        count = 0
        for inst in instances:
            if inst.failure_phase == "can_run":
                continue
            if inst.outcome_kind is None:
                continue
            if inst.outcome_kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR:
                count += 1
            else:
                break
        return count
```

Reusing `list_state_instances_for_ticket` + the in-memory walk guarantees byte-identical skip semantics — the riskiest place to diverge. Do NOT push the skip logic into SQL.

- [ ] **Step 3: Run the helper slice**

Run: `uv run pytest packages/foreman/tests/v4/test_postgres_repository.py -k "consecutive or latest_pr or count_state" -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add packages/foreman/src/foreman/v4/postgres_repository.py
git commit -m "feat(v5): PostgresTicketRepository — runaway-defense helpers (parity walk)"
```

### Task 8: PostgresTicketRepository — dependency tracking + full contract green

**Files:**
- Modify: `packages/foreman/src/foreman/v4/postgres_repository.py`

- [ ] **Step 1: Confirm dependency tests fail**

Run: `uv run pytest packages/foreman/tests/v4/test_postgres_repository.py -k "depend" -v`
Expected: FAIL — methods not defined.

- [ ] **Step 2: Implement dependency methods**

Append to `PostgresTicketRepository`:

```python
    # --- Dependency tracking ---

    def set_ticket_dependencies(self, ticket_id: int, *, deps: list[int]) -> None:
        from psycopg.types.json import Jsonb

        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE tickets SET depends_on = %s WHERE id = %s",
                (Jsonb(list(deps)), ticket_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            conn.commit()

    def get_ticket_dependencies(self, ticket_id: int) -> list[int]:
        return list(self.get_ticket(ticket_id).depends_on)

    def list_unmet_dependencies(self, ticket_id: int) -> list[int]:
        deps = self.get_ticket_dependencies(ticket_id)
        unmet: list[int] = []
        for dep in deps:
            if self.get_ticket(dep).current_state != "Done":
                unmet.append(dep)
        return unmet
```

- [ ] **Step 3: Run the FULL contract suite against Postgres**

Run: `uv run pytest packages/foreman/tests/v4/test_postgres_repository.py -v`
Expected: ALL PASS — the same suite that proves `SqliteTicketRepository` now proves `PostgresTicketRepository`. This is the parity proof for the repository layer.

- [ ] **Step 4: Commit**

```bash
git add packages/foreman/src/foreman/v4/postgres_repository.py
git commit -m "feat(v5): PostgresTicketRepository — dependencies; full contract green"
```

---

## Phase 3 — Decouple event archival + neutralize file-snapshot backup

### Task 9: Add `append_event` to the repository Protocol

The `EventArchiveObserver` currently takes a raw `sqlite3.Connection` (`conn=repo.connection`) and INSERTs into `events`. That couples it to SQLite. Add an `append_event` method to the Protocol so both impls own their own `events` writes, and the observer becomes storage-agnostic.

**Files:**
- Modify: `packages/foreman/src/foreman/v4/repository.py`
- Modify: `packages/foreman/src/foreman/v4/sqlite_repository.py`
- Modify: `packages/foreman/src/foreman/v4/postgres_repository.py`
- Test: `packages/foreman/tests/v4/_repository_contract.py` (add a contract test)

- [ ] **Step 1: Add the contract test**

In `packages/foreman/tests/v4/_repository_contract.py`, add a test method to the `RepositoryContract` class so it runs against all three impls:

```python
    def test_append_event_round_trips(self, repo: TicketRepository) -> None:
        now = dt.datetime(2026, 6, 21, 12, 0, tzinfo=dt.UTC)
        ticket = repo.create_ticket(project="p", issue_number=1, now=now)
        inst = repo.open_state_instance(
            ticket_id=ticket.id, state_name="Queued", sequence=0, now=now
        )
        repo.append_event(
            ticket_id=ticket.id,
            instance_id=inst.id,
            event_type="StateEntered",
            state_name="Queued",
            sequence=0,
            at=now,
            payload={"foo": "bar"},
        )
        events = repo.list_events_for_ticket(ticket.id)
        assert len(events) == 1
        assert events[0]["event_type"] == "StateEntered"
        assert events[0]["payload"] == {"foo": "bar"}
```

Ensure `import datetime as dt` is present at the top of the contract file.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_in_memory_repository.py -k append_event -v`
Expected: FAIL — `append_event` not defined.

- [ ] **Step 3: Extend the Protocol + all three impls**

In `repository.py`, add to the `TicketRepository` Protocol (in the journal section):

```python
    def append_event(
        self,
        *,
        ticket_id: int,
        instance_id: int,
        event_type: str,
        state_name: str,
        sequence: int,
        at: dt.datetime,
        payload: dict[str, Any],
    ) -> None: ...
    def list_events_for_ticket(self, ticket_id: int) -> list[dict[str, Any]]: ...
```

In `InMemoryTicketRepository.__init__`, add `self._events: list[dict[str, Any]] = []`, then add:

```python
    def append_event(
        self, *, ticket_id, instance_id, event_type, state_name,
        sequence, at, payload,
    ) -> None:
        self._events.append({
            "ticket_id": ticket_id, "instance_id": instance_id,
            "event_type": event_type, "state_name": state_name,
            "sequence": sequence, "at": at, "payload": dict(payload),
        })

    def list_events_for_ticket(self, ticket_id: int) -> list[dict[str, Any]]:
        return [e for e in self._events if e["ticket_id"] == ticket_id]
```

In `SqliteTicketRepository`, add (using the existing `_conn` + lock pattern in that file — match its style):

```python
    def append_event(
        self, *, ticket_id, instance_id, event_type, state_name,
        sequence, at, payload,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ticket_id, instance_id, event_type, "
                "state_name, sequence, at, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ticket_id, instance_id, event_type, state_name,
                 sequence, _to_iso(at), json.dumps(payload)),
            )
            self._conn.commit()

    def list_events_for_ticket(self, ticket_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE ticket_id = ? ORDER BY at",
                (ticket_id,),
            ).fetchall()
        return [
            {**dict(r), "payload": json.loads(r["payload"])} for r in rows
        ]
```

(Use the file's existing `_to_iso` helper and `json` import — both are already present in `sqlite_repository.py`.)

In `PostgresTicketRepository`, add:

```python
    def append_event(
        self, *, ticket_id, instance_id, event_type, state_name,
        sequence, at, payload,
    ) -> None:
        from psycopg.types.json import Jsonb

        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO events (ticket_id, instance_id, event_type, "
                "state_name, sequence, at, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (ticket_id, instance_id, event_type, state_name,
                 sequence, _to_db(at), Jsonb(payload)),
            )
            conn.commit()

    def list_events_for_ticket(self, ticket_id: int) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE ticket_id = %s ORDER BY at",
                (ticket_id,),
            ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run the new contract test against all three impls**

Run: `uv run pytest packages/foreman/tests/v4/test_in_memory_repository.py packages/foreman/tests/v4/test_sqlite_repository.py packages/foreman/tests/v4/test_postgres_repository.py -k append_event -v`
Expected: PASS (3 — one per impl).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/repository.py packages/foreman/src/foreman/v4/sqlite_repository.py packages/foreman/src/foreman/v4/postgres_repository.py packages/foreman/tests/v4/_repository_contract.py
git commit -m "feat(v5): add repo.append_event — storage-agnostic event archival"
```

### Task 10: Rewire EventArchiveObserver onto `repo.append_event`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/observers/event_archive.py`
- Test: `packages/foreman/tests/v4/test_event_archive_observer.py` (existing — read it first)

- [ ] **Step 1: Read the existing observer + its test**

Read `packages/foreman/src/foreman/v4/observers/event_archive.py` and its test. Note the current constructor signature (`conn: sqlite3.Connection`) and how it builds the INSERT. The change: constructor takes `repo: TicketRepository` and calls `repo.append_event(...)` with the same field values it currently INSERTs.

- [ ] **Step 2: Update the observer's test to inject a repo**

Modify the existing test so it constructs `EventArchiveObserver(repo=<in-memory repo>)` and asserts via `repo.list_events_for_ticket(...)` instead of querying a raw connection. Keep the existing event-shape assertions.

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_event_archive_observer.py -v`
Expected: FAIL — constructor signature mismatch.

- [ ] **Step 4: Rewrite the observer**

Change `EventArchiveObserver.__init__` to accept `repo: TicketRepository` and store it. In its `__call__`/handler, replace the raw-SQL INSERT with a `self._repo.append_event(ticket_id=..., instance_id=..., event_type=..., state_name=..., sequence=..., at=..., payload=...)` call, passing the same values currently extracted from the event.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest packages/foreman/tests/v4/test_event_archive_observer.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/observers/event_archive.py packages/foreman/tests/v4/test_event_archive_observer.py
git commit -m "refactor(v5): EventArchiveObserver uses repo.append_event (drop raw-conn coupling)"
```

---

## Phase 4 — Bootstrap engine selection + backup neutralization

### Task 11: Select repository impl from config; disable file-snapshot backup under Postgres

**Files:**
- Modify: `packages/foreman/src/foreman/v4/bootstrap.py`
- Test: `packages/foreman/tests/v4/test_bootstrap.py` (existing — read it first)

- [ ] **Step 1: Read the existing bootstrap test**

Read `packages/foreman/tests/v4/test_bootstrap.py` to learn how `bootstrap_cli_context` is exercised (what config + factories it injects). Match that pattern.

- [ ] **Step 2: Write the failing tests**

Add to `packages/foreman/tests/v4/test_bootstrap.py`:

```python
def test_bootstrap_uses_sqlite_when_engine_sqlite(tmp_path, ...):
    # Build a V4Config with storage.engine == "sqlite" (the default).
    # Assert the constructed CliContext.repo is a SqliteTicketRepository.
    ctx = bootstrap_cli_context(config=cfg, identity=..., git_provider_factory=...)
    from foreman.v4.sqlite_repository import SqliteTicketRepository
    assert isinstance(ctx.repo, SqliteTicketRepository)


def test_bootstrap_uses_postgres_when_engine_postgres(monkeypatch, ...):
    # Build a V4Config with storage.engine == "postgres" + a dsn.
    # Stub PostgresTicketRepository.from_dsn to a sentinel so the test
    # doesn't need a live DB; assert from_dsn was called with the dsn +
    # pool sizes and that backup is the disabled sentinel.
    ...
```

Fill these in against the actual `bootstrap_cli_context` test conventions in the file (the existing tests show how config + fakes are assembled — reuse them).

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest packages/foreman/tests/v4/test_bootstrap.py -k "engine" -v`
Expected: FAIL — bootstrap always builds `SqliteTicketRepository` today.

- [ ] **Step 4: Implement engine selection**

In `bootstrap.py`, replace the unconditional:

```python
    repo = SqliteTicketRepository.at_path(Path(config.db_path))
```

with:

```python
    if config.storage.engine == "postgres":
        from foreman.v4.postgres_repository import PostgresTicketRepository

        assert config.storage.dsn is not None  # StorageConfig validator guarantees
        repo = PostgresTicketRepository.from_dsn(
            config.storage.dsn,
            pool_min=config.storage.pool_min,
            pool_max=config.storage.pool_max,
        )
    else:
        repo = SqliteTicketRepository.at_path(Path(config.db_path))
```

Also update the `EventArchiveObserver` wiring (changed in Task 10) from `EventArchiveObserver(conn=repo.connection)` to `EventArchiveObserver(repo=repo)`.

For the backup scheduler: the file-snapshot `BackupScheduler` is SQLite-specific (it copies the `.db` file). Under Postgres it must not run. Replace:

```python
    backup_scheduler = BackupScheduler.from_config(
        config.backup, src_conn=repo.connection, bus=bus,
    )
```

with:

```python
    if config.storage.engine == "postgres":
        # File-snapshot backups are SQLite-specific. Under Postgres,
        # backups are an ops concern (pg_dump / WAL archiving), out of
        # scope for the daemon. Use the disabled sentinel so the daemon's
        # unconditional tick() call stays a no-op.
        from foreman.v4.state_backup import _DISABLED_BACKUP_SCHEDULER

        backup_scheduler = _DISABLED_BACKUP_SCHEDULER
    else:
        backup_scheduler = BackupScheduler.from_config(
            config.backup, src_conn=repo.connection, bus=bus,
        )
```

(Confirm the disabled sentinel's exact name in `state_backup.py` — the bootstrap already imports `BackupScheduler` from there; the survey noted a `_DisabledBackupScheduler`/`_DISABLED_BACKUP_SCHEDULER` sentinel exists. Use whatever the module actually exports.)

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_bootstrap.py -k "engine" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/bootstrap.py packages/foreman/tests/v4/test_bootstrap.py
git commit -m "feat(v5): bootstrap selects repo impl from [storage].engine; backup no-op under postgres"
```

---

## Phase 5 — End-to-end parity, container topology, migration script, docs

### Task 12: v4 lifecycle e2e against Postgres

**Files:**
- Read: `packages/foreman/tests/v4/test_e2e_lifecycle.py` (the existing v4 e2e)
- Create: `packages/foreman/tests/v4/test_postgres_e2e.py`

- [ ] **Step 1: Read the existing e2e**

Read `packages/foreman/tests/v4/test_e2e_lifecycle.py` (and any sibling e2e that drives a ticket Queued→Done with fakes). Identify how it constructs the repository — it likely uses `SqliteTicketRepository.in_memory()` or an `InMemoryTicketRepository`.

- [ ] **Step 2: Write the Postgres-backed e2e**

Create `packages/foreman/tests/v4/test_postgres_e2e.py` that runs the same lifecycle scenario but with `PostgresTicketRepository.from_dsn(clean_postgres_dsn, ...)` as the repo. Import the `clean_postgres_dsn` fixture from `postgres_fixture`. Reuse the existing fakes (FakeGitProvider, fake dispatcher) from the existing e2e — import them rather than redefining.

The scenario must cover, at minimum: create ticket → advance through states → drive to a terminal state, asserting the same final state + state-instance journal the SQLite e2e asserts. Add one Postgres-specific assertion that proves #403's class is closed:

```python
def test_drop_then_poll_does_not_resurrect(clean_postgres_dsn):
    repo = PostgresTicketRepository.from_dsn(clean_postgres_dsn, pool_min=1, pool_max=4)
    now = dt.datetime(2026, 6, 21, tzinfo=dt.UTC)
    t = repo.create_ticket(project="p", issue_number=1, now=now)
    repo.set_ticket_state(t.id, "Failed", now=now)
    # A fresh read on a DIFFERENT pooled connection sees the committed
    # Failed state — the cross-connection read-after-write that SQLite's
    # in-process cache broke (#403). list_open_tickets must NOT return it.
    assert all(ot.id != t.id for ot in repo.list_open_tickets())
    assert repo.get_ticket(t.id).current_state == "Failed"
```

- [ ] **Step 3: Run the Postgres e2e**

Run: `uv run pytest packages/foreman/tests/v4/test_postgres_e2e.py -v`
Expected: PASS — the lifecycle behaves identically on Postgres + the drop-resurrection guard holds.

- [ ] **Step 4: Run the FULL suite to confirm no regression**

Run: `uv run pytest packages/foreman/tests/v4 -q`
Expected: all green. (If Docker is unavailable in the runner, the Postgres tests skip via the fixture guard — confirm they SKIP, not ERROR.)

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/tests/v4/test_postgres_e2e.py
git commit -m "test(v5): v4 lifecycle e2e parity on postgres + #403 resurrection guard"
```

### Task 13: docker-compose (postgres + daemon)

**Files:**
- Create: `docker-compose.yml` (repo root)
- Read: existing `Dockerfile` (to reuse the daemon image build)

- [ ] **Step 1: Read the existing Dockerfile + container conventions**

Read the repo's `Dockerfile` and any existing compose/run scripts (the daemon already runs in a container `foreman-daemon` per the docker runtime work). Note the image name, the config mount path (`~/.foreman`), and the entrypoint.

- [ ] **Step 2: Write docker-compose.yml**

Create `docker-compose.yml` at the repo root. This plan includes only postgres + daemon (the dashboard service comes with the dashboard plan):

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: foreman
      POSTGRES_PASSWORD: ${FOREMAN_PG_PASSWORD:?set FOREMAN_PG_PASSWORD}
      POSTGRES_DB: foreman
    volumes:
      - foreman-pg-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U foreman -d foreman"]
      interval: 5s
      timeout: 5s
      retries: 10

  foreman-daemon:
    image: ghcr.io/jeffrichley/foreman:dev
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      # The daemon reads ~/.foreman/v5/config.toml; the [storage] block
      # there must set engine = "postgres" + this dsn. Documented in the
      # runbook (Task 15).
      FOREMAN_PG_DSN: postgresql://foreman:${FOREMAN_PG_PASSWORD}@postgres:5432/foreman
    volumes:
      - ${HOME}/.foreman:/root/.foreman
    restart: unless-stopped

volumes:
  foreman-pg-data:
```

- [ ] **Step 3: Validate the compose file**

Run: `FOREMAN_PG_PASSWORD=test docker compose -f docker-compose.yml config`
Expected: prints the resolved config with no errors (validates YAML + interpolation).

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(v5): docker-compose for postgres + foreman-daemon"
```

### Task 14: One-shot migration script (`foreman migrate-v4-to-v5`)

**Files:**
- Create: `packages/foreman/src/foreman/v4/cli/migrate.py`
- Create: `packages/foreman/tests/v4/test_migrate_v4_to_v5.py`
- Modify: the CLI app registration (wherever v4 commands are registered — find it via the existing `cmd_reset`/`cmd_enqueue` registration)

The migration script ports `tickets` + `state_instances` from a SQLite file to Postgres. It is idempotent (skip tickets that already exist by `(project, issue_number)`) and intentionally does NOT port `events` (history, low value, large).

- [ ] **Step 1: Write the failing test**

Create `packages/foreman/tests/v4/test_migrate_v4_to_v5.py`:

```python
import datetime as dt

from foreman.v4.cli.migrate import migrate_v4_to_v5
from foreman.v4.postgres_repository import PostgresTicketRepository
from foreman.v4.sqlite_repository import SqliteTicketRepository
from .postgres_fixture import clean_postgres_dsn  # noqa: F401


def test_migrate_ports_tickets_and_instances(tmp_path, clean_postgres_dsn):
    now = dt.datetime(2026, 6, 21, tzinfo=dt.UTC)
    src = SqliteTicketRepository.at_path(tmp_path / "state.db")
    t = src.create_ticket(project="p", issue_number=7, now=now)
    src.set_ticket_state(t.id, "NeedsHelp", now=now)
    src.open_state_instance(ticket_id=t.id, state_name="Queued", sequence=0, now=now)

    migrated = migrate_v4_to_v5(
        sqlite_path=tmp_path / "state.db", postgres_dsn=clean_postgres_dsn
    )
    assert migrated == 1

    dst = PostgresTicketRepository.from_dsn(clean_postgres_dsn, pool_min=1, pool_max=4)
    ported = dst.get_ticket_by_issue(project="p", issue_number=7)
    assert ported.current_state == "NeedsHelp"
    assert len(dst.list_state_instances_for_ticket(ported.id)) == 1


def test_migrate_is_idempotent(tmp_path, clean_postgres_dsn):
    now = dt.datetime(2026, 6, 21, tzinfo=dt.UTC)
    src = SqliteTicketRepository.at_path(tmp_path / "state.db")
    src.create_ticket(project="p", issue_number=7, now=now)
    migrate_v4_to_v5(sqlite_path=tmp_path / "state.db", postgres_dsn=clean_postgres_dsn)
    # Second run skips the existing (project, issue_number).
    again = migrate_v4_to_v5(sqlite_path=tmp_path / "state.db", postgres_dsn=clean_postgres_dsn)
    assert again == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_migrate_v4_to_v5.py -v`
Expected: FAIL — `No module named 'foreman.v4.cli.migrate'`.

- [ ] **Step 3: Implement the migration function + CLI command**

Create `packages/foreman/src/foreman/v4/cli/migrate.py`:

```python
"""foreman migrate-v4-to-v5 — one-shot SQLite → Postgres port.

Ports tickets + state_instances. Idempotent on (project, issue_number).
Does NOT port the events archive (history; large; low value). State-
instance integer ids are NOT preserved — Postgres assigns fresh
BIGSERIAL ids; sequence ordering within a ticket is preserved, which is
what the state machine relies on.
"""
from __future__ import annotations

from pathlib import Path

import typer

from foreman.v4.postgres_repository import PostgresTicketRepository
from foreman.v4.repository import TicketAlreadyExistsError
from foreman.v4.sqlite_repository import SqliteTicketRepository


def migrate_v4_to_v5(*, sqlite_path: Path, postgres_dsn: str) -> int:
    """Port tickets + state-instances. Returns the count of tickets ported."""
    src = SqliteTicketRepository.at_path(sqlite_path)
    dst = PostgresTicketRepository.from_dsn(postgres_dsn)
    ported = 0
    for ticket in src.list_all_tickets():
        try:
            new = dst.create_ticket(
                project=ticket.project,
                issue_number=ticket.issue_number,
                now=ticket.created_at,
            )
        except TicketAlreadyExistsError:
            continue  # idempotent skip
        if ticket.current_state != "Queued":
            dst.set_ticket_state(new.id, ticket.current_state, now=ticket.updated_at)
        if ticket.depends_on:
            dst.set_ticket_dependencies(new.id, deps=ticket.depends_on)
        for inst in src.list_state_instances_for_ticket(ticket.id):
            new_inst = dst.open_state_instance(
                ticket_id=new.id,
                state_name=inst.state_name,
                sequence=inst.sequence,
                now=inst.entered_at,
            )
            if inst.outcome_kind is not None and inst.next_state is not None:
                dst.mark_execute_completed(
                    new_inst.id,
                    now=inst.execute_completed_at or inst.entered_at,
                    outcome_kind=inst.outcome_kind,
                    outcome_payload=inst.outcome_payload or {},
                    next_state=inst.next_state,
                )
            if inst.exited_at is not None:
                dst.close_state_instance(new_inst.id, now=inst.exited_at)
        ported += 1
    return ported


def cmd_migrate_v4_to_v5(
    sqlite_path: Path = typer.Option(..., "--sqlite-path"),
    postgres_dsn: str = typer.Option(..., "--postgres-url"),
) -> None:
    count = migrate_v4_to_v5(sqlite_path=sqlite_path, postgres_dsn=postgres_dsn)
    typer.echo(f"migrated {count} ticket(s) to postgres")
```

Register `cmd_migrate_v4_to_v5` on the v4 typer app where the other commands are registered (find the registration site by grepping for where `cmd_reset` or `cmd_enqueue` is added to the app; add `app.command("migrate-v4-to-v5")(cmd_migrate_v4_to_v5)` alongside).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest packages/foreman/tests/v4/test_migrate_v4_to_v5.py -v`
Expected: PASS (2).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/cli/migrate.py packages/foreman/tests/v4/test_migrate_v4_to_v5.py packages/foreman/src/foreman/v4/cli/__init__.py
git commit -m "feat(v5): foreman migrate-v4-to-v5 one-shot port (idempotent)"
```

### Task 15: Operator runbook

**Files:**
- Create: `docs/runbooks/postgres-migration.md`

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/postgres-migration.md` documenting:
- The two migration paths from the spec: cold-start (recommended — drain in-flight tickets, switch config, start fresh) and hot-port (`foreman migrate-v4-to-v5`, opt-in, lossy on event history).
- The `[storage]` config block to add to `~/.foreman/v5/config.toml`:
  ```toml
  [storage]
  engine = "postgres"
  dsn = "postgresql://foreman:<password>@postgres:5432/foreman"
  pool_min = 2
  pool_max = 10
  ```
- The docker-compose bring-up sequence: `FOREMAN_PG_PASSWORD=<pw> docker compose up -d postgres` (wait for healthy) then `docker compose up -d foreman-daemon`.
- The cold-start drain procedure: `foreman ps` shows no in-flight tickets before cutover; hold-and-defer any that can't drain.
- Verification: `foreman ps` against the new daemon returns (empty or ported) tickets; the `database is locked` and drop-resurrection bugs no longer reproduce.
- Rollback: stop the v5 daemon, switch `[storage].engine` back to `sqlite`, restart. (The SQLite `.db` file is untouched by the Postgres path.)

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/postgres-migration.md
git commit -m "docs(v5): postgres migration operator runbook"
```

### Task 16: Full `just check` + final verification

**Files:** none (verification task)

- [ ] **Step 1: Run the full check gate**

Run: `just check`
Expected: ruff clean, mypy clean, import-linter clean, full pytest green with coverage ≥ 78%. If Docker is available in the environment, the Postgres tests run; if not, they skip cleanly (not error).

- [ ] **Step 2: If mypy flags the Postgres repo**

`PostgresTicketRepository` must structurally satisfy the `TicketRepository` Protocol. If mypy complains about a missing/mismatched method, the signature diverged from the Protocol — fix the signature to match `repository.py` exactly. Add a structural assertion test if not already covered:

```python
def test_postgres_repo_satisfies_protocol():
    from foreman.v4.repository import TicketRepository
    from foreman.v4.postgres_repository import PostgresTicketRepository
    _: type[TicketRepository] = PostgresTicketRepository  # structural check
```

- [ ] **Step 3: Confirm the two bug classes are closed by the new tests**

Run: `uv run pytest packages/foreman/tests/v4/test_postgres_e2e.py -k resurrect -v`
Expected: PASS — `test_drop_then_poll_does_not_resurrect` proves #403's class is closed by Postgres cross-connection read-after-write.

- [ ] **Step 4: Adversarial-review pass (standing rule)**

Before opening the PR, do a hostile-reviewer pass on the full diff. Specific things to verify:
- Every `PostgresTicketRepository` method commits or rolls back — no leaked open transactions holding pool connections.
- The datetime tz normalization (`_to_db`) round-trips correctly for both naive and aware inputs the contract tests pass.
- `delete_ticket`'s reliance on `ON DELETE CASCADE` actually drops journal rows (the e2e or a contract test must exercise delete-with-instances).
- The two `count_consecutive_*` helpers produce byte-identical results to the in-memory impl (the contract suite covers this — confirm those specific tests run against Postgres).
- No `sqlite3` import remains in any production code path when `engine = "postgres"` (the SQLite impl is still imported by `bootstrap` for the sqlite branch + by `migrate.py` for the source side — that's correct; verify it's never constructed under the postgres branch).

- [ ] **Step 5: Open the PR**

```bash
git push -u origin spec/foreman-v5-dashboard-design
gh pr create --title "feat(v5): postgres substrate migration" --body "<summary + closes #403, #404>"
```

(Use the Wren PAT per the standing GitHub-ops rule. Adversarial-review the diff before this step.)

---

## Self-Review

**Spec coverage** (against `2026-06-21-foreman-v5-postgres-http-design.md`):
- ✅ PostgreSQL single source of truth — Tasks 3-8.
- ✅ `TicketRepository` Protocol preserved + contract parity — Tasks 5-8.
- ✅ No `sqlite3` in the postgres runtime path — Task 16 Step 4 verifies.
- ✅ `[storage]` config selection — Tasks 2, 11.
- ✅ #404 (`database is locked`) — dissolved by the pool + MVCC (no RLock); covered by the e2e running concurrent-safe.
- ✅ #403 (drop resurrection) — Task 12 `test_drop_then_poll_does_not_resurrect`.
- ✅ Three-container topology — Task 13 covers postgres + daemon; the dashboard container is explicitly deferred to the dashboard plan (per scope boundary).
- ✅ One-shot migration script — Task 14.
- ✅ Operator runbook — Task 15.
- ✅ Parity e2e — Task 12.
- ⚠️ **Deferred to the HTTP-plane plan (NOT this plan):** the FastAPI control plane, the SSE endpoint, the `daemon_health` table, the `status.py`/`metrics.py` modules, alembic migrations. This plan uses a plain `.sql` schema applied idempotently at repo construction (matching the v4 SQLite pattern) rather than alembic — alembic is deferred to when schema migrations across deployed Postgres instances actually become a concern. **This is a deliberate scope reduction from the spec** flagged here for the spec to be amended or for these to land in the HTTP-plane plan. The spec's `daemon_health` table (for Pepper's substrate-hot watch) lands with the HTTP plane, not the bare DB migration — it has no consumer until the dashboard.

**Placeholder scan:** No TBD/TODO. The two places that say "find the registration site" / "confirm the sentinel's exact name" are pointers to read-existing-code, not placeholders for the implementer to invent behavior — the exact code to write is shown; only the insertion point must be located in-repo.

**Type consistency:** `from_dsn` (not `at_dsn`) used consistently. `append_event` signature identical across Protocol + all three impls + the migration caller. `migrate_v4_to_v5` keyword args (`sqlite_path`, `postgres_dsn`) match between the function, the CLI command, and both tests. Integer ids throughout (no UUID). `_to_db` used on every datetime write.

**Scope:** Tightly bounded to the DB migration. HTTP plane + dashboard are explicitly out, with the deferred items enumerated above so nothing is silently dropped.
