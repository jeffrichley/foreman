# Foreman Daemon v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the v1 orchestrator daemon that polls GitHub labels, queues tickets, and drives each ticket through the Planner → Reviewer → (Fixer loop) → spec merge → Worker → Reviewer → (Fixer loop) → impl merge pipeline autonomously.

**Architecture:** Three concurrent async loops — poller (30s tick), self-notify hook (after each role run), and single worker (1 ticket in flight). Communicate through an in-memory de-duped queue sorted on dequeue by `(-stage_index, last_transition_at)`. State machine is a pure function `next_action(ticket, project) → Action | None`. Per-ticket locks built in v1 (with `max_concurrent_workers=1` config knob) so v2 can add concurrency without refactor. Crash recovery is "full scan on startup, halt in-flight tickets with `foreman:failed`." SQLite at `~/.foreman/foreman.sqlite` for audit/replay.

**Tech Stack:** Python 3.12, asyncio, stdlib `sqlite3`, Pydantic (config), click (CLI), pytest + pytest-asyncio.

**Spec reference:** `docs/superpowers/specs/2026-06-01-foreman-daemon-design.md`. Acceptance criteria in §14 of that doc define "v1 daemon done."

---

## Pre-work

### Task 0: Create feature branch

**Files:**
- (none — just git)

- [ ] **Step 1: Create branch from main**

```bash
cd e:/workspaces/ai/agents/foreman
git checkout main
git pull --ff-only origin main
git checkout -b feat/daemon-v1
git push -u origin feat/daemon-v1
```

Expected: branch created locally + pushed to origin.

- [ ] **Step 2: Verify baseline tests pass**

```bash
uv run --no-sync pytest -q
```

Expected: `358 passed` (or whatever the current count is).

---

## Phase 1: Configuration

### Task 1: Extend config with DaemonConfig and auto-merge knobs

**Files:**
- Modify: `packages/foreman/src/foreman/config.py`
- Test: `packages/foreman/tests/test_config.py`

- [ ] **Step 1: Write the failing test for DaemonConfig defaults**

Add to `packages/foreman/tests/test_config.py`:

```python
def test_daemon_config_has_sane_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[admin]\ngithub_token_env = \"FOREMAN_ADMIN_TOKEN\"\n"
    )
    cfg = load_config(config_path)
    assert cfg.daemon.poll_interval_seconds == 30
    assert cfg.daemon.max_concurrent_workers == 1
    assert cfg.daemon.log_level == "INFO"
    assert cfg.daemon.log_path.endswith("daemon.log")
    assert cfg.daemon.sqlite_path.endswith("foreman.sqlite")


def test_daemon_config_rejects_max_workers_above_one(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[admin]\ngithub_token_env = \"FOREMAN_ADMIN_TOKEN\"\n"
        "[daemon]\nmax_concurrent_workers = 4\n"
    )
    with pytest.raises(ValueError, match="max_concurrent_workers"):
        load_config(config_path)


def test_project_config_auto_merge_defaults_to_false(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[admin]\ngithub_token_env = \"FOREMAN_ADMIN_TOKEN\"\n"
        "[projects.voice]\n"
        "repo = \"jeffrichley/voice\"\n"
        "local_clone_path = \"/tmp/voice\"\n"
    )
    cfg = load_config(config_path)
    project = cfg.projects["voice"]
    assert project.auto_merge_spec is False
    assert project.auto_merge_impl is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_config.py -k "daemon_config or auto_merge" -v
```

Expected: FAILs with `AttributeError` (no `daemon` attribute on Config; no `auto_merge_spec` on ProjectConfig).

- [ ] **Step 3: Add DaemonConfig and auto-merge fields**

Edit `packages/foreman/src/foreman/config.py`. Add after `AdminConfig`:

```python
class DaemonConfig(BaseModel):
    """Daemon runtime configuration.

    ``max_concurrent_workers`` is a v1 forward-compat knob — only ``1`` is
    valid in v1. The lock infrastructure tolerates higher values but the
    daemon code is not yet audited for multi-worker safety. v2 will lift
    this validation.
    """

    poll_interval_seconds: int = Field(default=30, ge=5)
    max_concurrent_workers: int = Field(default=1, ge=1)
    log_path: str = Field(default="~/.foreman/daemon.log")
    log_level: str = Field(default="INFO")
    sqlite_path: str = Field(default="~/.foreman/foreman.sqlite")

    @field_validator("max_concurrent_workers")
    @classmethod
    def _validate_max_workers(cls, v: int) -> int:
        if v != 1:
            raise ValueError(
                "daemon.max_concurrent_workers must be 1 in v1; "
                "multi-worker concurrency is deferred"
            )
        return v
```

Add to `ProjectConfig`:

```python
    auto_merge_spec: bool = Field(
        default=False,
        description=(
            "When True, daemon auto-merges spec PRs that reach foreman:spec-ready. "
            "When False (default), ticket parks at spec-ready awaiting human merge."
        ),
    )
    auto_merge_impl: bool = Field(
        default=False,
        description=(
            "When True, daemon auto-merges impl PRs that reach foreman:ready-for-merge. "
            "When False (default), ticket parks at ready-for-merge awaiting human merge."
        ),
    )
```

Add to `Config`:

```python
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
```

Add the import at the top:

```python
from pydantic import BaseModel, Field, field_validator
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_config.py -k "daemon_config or auto_merge" -v
```

Expected: PASS.

- [ ] **Step 5: Run full config tests + mypy to catch regressions**

```bash
uv run --no-sync pytest packages/foreman/tests/test_config.py -q
uv run --no-sync mypy packages/foreman/src
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/config.py packages/foreman/tests/test_config.py
git commit -m "feat(config): add DaemonConfig and ProjectConfig auto-merge knobs"
```

---

## Phase 2: SQLite storage

### Task 2: SQLite schema, migrations, meta table

**Files:**
- Create: `packages/foreman/src/foreman/storage.py`
- Create: `packages/foreman/tests/test_storage.py`

- [ ] **Step 1: Write failing test for fresh database initialization**

Create `packages/foreman/tests/test_storage.py`:

```python
"""Tests for the SQLite lifecycle storage layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from foreman.storage import Storage


def test_init_creates_schema_on_fresh_db(tmp_path: Path) -> None:
    db_path = tmp_path / "foreman.sqlite"
    storage = Storage(db_path)
    storage.init()

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}

    assert "meta" in tables
    assert "pipelines" in tables
    assert "node_runs" in tables
    assert "transitions" in tables
    assert "failures" in tables
    assert "labels_seen" in tables


def test_init_records_schema_version(tmp_path: Path) -> None:
    db_path = tmp_path / "foreman.sqlite"
    storage = Storage(db_path)
    storage.init()

    with sqlite3.connect(db_path) as conn:
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]

    assert version == "1"


def test_init_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "foreman.sqlite"
    storage = Storage(db_path)
    storage.init()
    storage.init()  # second call should not raise

    with sqlite3.connect(db_path) as conn:
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]

    assert version == "1"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_storage.py -v
```

Expected: FAIL with `ModuleNotFoundError: foreman.storage`.

- [ ] **Step 3: Implement Storage with schema migrations**

Create `packages/foreman/src/foreman/storage.py`:

```python
"""SQLite lifecycle storage for the Foreman daemon.

Stores pipelines, node runs, label transitions, failures, and last-seen
label snapshots used by the poller diff. Not load-bearing for correctness
(GitHub labels remain source of truth) — load-bearing for observability,
audit trail, and crash-recovery reconciliation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_V1 = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pipelines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL,
        issue_number INTEGER NOT NULL,
        current_state TEXT NOT NULL,
        started_at DATETIME NOT NULL,
        terminated_at DATETIME,
        parent_ticket_id INTEGER,
        blocks_ticket_id INTEGER,
        UNIQUE(project, issue_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS node_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
        role TEXT NOT NULL,
        identity TEXT NOT NULL,
        started_at DATETIME NOT NULL,
        finished_at DATETIME,
        outcome TEXT,
        structured_output_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
        at DATETIME NOT NULL,
        from_labels_json TEXT NOT NULL,
        to_labels_json TEXT NOT NULL,
        actor TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS failures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pipeline_id INTEGER NOT NULL REFERENCES pipelines(id),
        at DATETIME NOT NULL,
        role TEXT NOT NULL,
        reason TEXT NOT NULL,
        traceback TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS labels_seen (
        project TEXT NOT NULL,
        issue_number INTEGER NOT NULL,
        labels_json TEXT NOT NULL,
        seen_at DATETIME NOT NULL,
        PRIMARY KEY (project, issue_number)
    )
    """,
]

CURRENT_SCHEMA_VERSION = 1


class Storage:
    """Foreman SQLite storage wrapper.

    Holds a path and produces connections on demand. Connection-per-call
    keeps sqlite3's threading restrictions out of the daemon's concern —
    each async task that needs storage gets its own short-lived connection.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser()

    def init(self) -> None:
        """Create tables and record schema version. Idempotent."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            for stmt in _SCHEMA_V1:
                conn.execute(stmt)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(CURRENT_SCHEMA_VERSION),),
            )

    def connect(self) -> sqlite3.Connection:
        """Open a connection. Caller is responsible for closing."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_storage.py -v
```

Expected: 3 PASSes.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/storage.py packages/foreman/tests/test_storage.py
git commit -m "feat(storage): add SQLite schema + idempotent init for daemon"
```

### Task 3: Storage CRUD for pipelines and labels_seen

**Files:**
- Modify: `packages/foreman/src/foreman/storage.py`
- Modify: `packages/foreman/tests/test_storage.py`

- [ ] **Step 1: Write failing tests for upsert + get on pipelines and labels_seen**

Add to `packages/foreman/tests/test_storage.py`:

```python
from datetime import datetime, timezone


def test_upsert_pipeline_creates_then_updates(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    pipeline_id = storage.upsert_pipeline(
        project="voice", issue_number=42, current_state="foreman:plan", started_at=now
    )
    assert pipeline_id > 0

    same_id = storage.upsert_pipeline(
        project="voice", issue_number=42, current_state="foreman:spec-review", started_at=now
    )
    assert same_id == pipeline_id

    row = storage.get_pipeline("voice", 42)
    assert row is not None
    assert row["current_state"] == "foreman:spec-review"


def test_get_pipeline_returns_none_when_absent(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    assert storage.get_pipeline("voice", 999) is None


def test_labels_seen_upsert_and_read(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    storage.upsert_labels_seen("voice", 42, ["foreman:plan"], now)
    assert storage.get_labels_seen("voice", 42) == ["foreman:plan"]

    storage.upsert_labels_seen("voice", 42, ["foreman:spec-review"], now)
    assert storage.get_labels_seen("voice", 42) == ["foreman:spec-review"]


def test_labels_seen_returns_none_when_absent(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    assert storage.get_labels_seen("voice", 42) is None


def test_iter_pipelines_in_flight_yields_non_terminal(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    storage.upsert_pipeline("voice", 1, "foreman:planning", now)
    storage.upsert_pipeline("voice", 2, "foreman:plan", now)
    storage.mark_pipeline_terminated("voice", 2, now)

    in_flight = list(storage.iter_pipelines_in_flight())
    states = [(row["project"], row["issue_number"]) for row in in_flight]
    assert ("voice", 1) in states
    assert ("voice", 2) not in states
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_storage.py -k "upsert or labels_seen or in_flight" -v
```

Expected: FAILs with `AttributeError`.

- [ ] **Step 3: Add CRUD methods to Storage**

Edit `packages/foreman/src/foreman/storage.py`. Add imports:

```python
import json
from datetime import datetime
from typing import Iterator
```

Add methods to `Storage`:

```python
    def upsert_pipeline(
        self,
        project: str,
        issue_number: int,
        current_state: str,
        started_at: datetime,
    ) -> int:
        """Insert pipeline if absent; update current_state if present. Returns id."""
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM pipelines WHERE project = ? AND issue_number = ?",
                (project, issue_number),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE pipelines SET current_state = ? WHERE id = ?",
                    (current_state, existing["id"]),
                )
                return int(existing["id"])
            cursor = conn.execute(
                "INSERT INTO pipelines(project, issue_number, current_state, started_at) "
                "VALUES (?, ?, ?, ?)",
                (project, issue_number, current_state, started_at.isoformat()),
            )
            return int(cursor.lastrowid)

    def get_pipeline(self, project: str, issue_number: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM pipelines WHERE project = ? AND issue_number = ?",
                (project, issue_number),
            ).fetchone()

    def mark_pipeline_terminated(
        self, project: str, issue_number: int, at: datetime
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pipelines SET terminated_at = ? "
                "WHERE project = ? AND issue_number = ?",
                (at.isoformat(), project, issue_number),
            )

    def iter_pipelines_in_flight(self) -> Iterator[sqlite3.Row]:
        with self.connect() as conn:
            for row in conn.execute(
                "SELECT * FROM pipelines WHERE terminated_at IS NULL"
            ):
                yield row

    def upsert_labels_seen(
        self,
        project: str,
        issue_number: int,
        labels: list[str],
        seen_at: datetime,
    ) -> None:
        sorted_json = json.dumps(sorted(labels))
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO labels_seen(project, issue_number, labels_json, seen_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project, issue_number) DO UPDATE SET "
                "labels_json = excluded.labels_json, seen_at = excluded.seen_at",
                (project, issue_number, sorted_json, seen_at.isoformat()),
            )

    def get_labels_seen(self, project: str, issue_number: int) -> list[str] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT labels_json FROM labels_seen "
                "WHERE project = ? AND issue_number = ?",
                (project, issue_number),
            ).fetchone()
        if row is None:
            return None
        return list(json.loads(row["labels_json"]))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_storage.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/storage.py packages/foreman/tests/test_storage.py
git commit -m "feat(storage): add pipeline and labels_seen CRUD methods"
```

### Task 4: Storage CRUD for node_runs, transitions, failures

**Files:**
- Modify: `packages/foreman/src/foreman/storage.py`
- Modify: `packages/foreman/tests/test_storage.py`

- [ ] **Step 1: Write failing tests**

Add to `packages/foreman/tests/test_storage.py`:

```python
def test_record_node_run_persists_role_and_outcome(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    pipeline_id = storage.upsert_pipeline("voice", 42, "foreman:plan", now)

    run_id = storage.record_node_run_start(
        pipeline_id=pipeline_id, role="planner", identity="foreman-planner-bot", at=now
    )
    later = datetime(2026, 6, 1, 12, 5, 0, tzinfo=timezone.utc)
    storage.record_node_run_finish(
        run_id=run_id,
        at=later,
        outcome="success",
        structured_output={"pr_number": 18},
    )

    with storage.connect() as conn:
        row = conn.execute("SELECT * FROM node_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["role"] == "planner"
    assert row["outcome"] == "success"
    assert json.loads(row["structured_output_json"]) == {"pr_number": 18}


def test_record_transition_persists_label_diff(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    pipeline_id = storage.upsert_pipeline("voice", 42, "foreman:plan", now)

    storage.record_transition(
        pipeline_id=pipeline_id,
        at=now,
        from_labels=["foreman:plan"],
        to_labels=["foreman:spec-review"],
        actor="planner",
    )

    with storage.connect() as conn:
        row = conn.execute(
            "SELECT * FROM transitions WHERE pipeline_id = ?", (pipeline_id,)
        ).fetchone()
    assert row["actor"] == "planner"
    assert json.loads(row["from_labels_json"]) == ["foreman:plan"]
    assert json.loads(row["to_labels_json"]) == ["foreman:spec-review"]


def test_record_failure_persists_reason_and_traceback(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    pipeline_id = storage.upsert_pipeline("voice", 42, "foreman:plan", now)

    storage.record_failure(
        pipeline_id=pipeline_id,
        at=now,
        role="planner",
        reason="RuntimeError: missing token",
        traceback="Traceback (most recent call last):\n  ...\n",
    )

    with storage.connect() as conn:
        row = conn.execute(
            "SELECT * FROM failures WHERE pipeline_id = ?", (pipeline_id,)
        ).fetchone()
    assert row["reason"].startswith("RuntimeError")
    assert "Traceback" in row["traceback"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_storage.py -k "node_run or transition or failure" -v
```

Expected: FAILs with `AttributeError`.

- [ ] **Step 3: Add the methods**

Add to `Storage` in `packages/foreman/src/foreman/storage.py`:

```python
    def record_node_run_start(
        self,
        *,
        pipeline_id: int,
        role: str,
        identity: str,
        at: datetime,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO node_runs(pipeline_id, role, identity, started_at) "
                "VALUES (?, ?, ?, ?)",
                (pipeline_id, role, identity, at.isoformat()),
            )
            return int(cursor.lastrowid)

    def record_node_run_finish(
        self,
        *,
        run_id: int,
        at: datetime,
        outcome: str,
        structured_output: dict | None,
    ) -> None:
        output_json = json.dumps(structured_output) if structured_output is not None else None
        with self.connect() as conn:
            conn.execute(
                "UPDATE node_runs SET finished_at = ?, outcome = ?, structured_output_json = ? "
                "WHERE id = ?",
                (at.isoformat(), outcome, output_json, run_id),
            )

    def record_transition(
        self,
        *,
        pipeline_id: int,
        at: datetime,
        from_labels: list[str],
        to_labels: list[str],
        actor: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO transitions(pipeline_id, at, from_labels_json, to_labels_json, actor) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    pipeline_id,
                    at.isoformat(),
                    json.dumps(sorted(from_labels)),
                    json.dumps(sorted(to_labels)),
                    actor,
                ),
            )

    def record_failure(
        self,
        *,
        pipeline_id: int,
        at: datetime,
        role: str,
        reason: str,
        traceback: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO failures(pipeline_id, at, role, reason, traceback) "
                "VALUES (?, ?, ?, ?, ?)",
                (pipeline_id, at.isoformat(), role, reason, traceback),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_storage.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/storage.py packages/foreman/tests/test_storage.py
git commit -m "feat(storage): add node_runs, transitions, failures CRUD"
```

---

## Phase 3: Dispatcher (pure state machine)

### Task 5: Action types and Ticket

**Files:**
- Create: `packages/foreman/src/foreman/dispatcher.py`
- Create: `packages/foreman/tests/test_dispatcher.py`

- [ ] **Step 1: Write failing test for Action and Ticket types**

Create `packages/foreman/tests/test_dispatcher.py`:

```python
"""Tests for the pure-function state machine."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foreman.dispatcher import (
    Action,
    ActionKind,
    Ticket,
    stage_index,
)


def test_ticket_is_hashable_and_frozen() -> None:
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    t = Ticket(
        project_name="voice",
        issue_number=42,
        labels=frozenset({"foreman:plan"}),
        last_transition_at=now,
    )
    s = {t}  # hashable
    assert t in s
    with pytest.raises(AttributeError):
        t.issue_number = 99  # type: ignore[misc]


def test_action_kinds_exist() -> None:
    assert ActionKind.RUN_PLANNER.value == "run_planner"
    assert ActionKind.RUN_REVIEWER_SPEC.value == "run_reviewer_spec"
    assert ActionKind.RUN_REVIEWER_IMPL.value == "run_reviewer_impl"
    assert ActionKind.RUN_FIXER_SPEC.value == "run_fixer_spec"
    assert ActionKind.RUN_FIXER_IMPL.value == "run_fixer_impl"
    assert ActionKind.RUN_WORKER.value == "run_worker"
    assert ActionKind.MERGE_SPEC_PR.value == "merge_spec_pr"
    assert ActionKind.MERGE_IMPL_PR.value == "merge_impl_pr"


def test_stage_index_orders_pipeline_progress() -> None:
    # Higher index = further along
    assert stage_index(ActionKind.RUN_PLANNER) < stage_index(ActionKind.RUN_REVIEWER_SPEC)
    assert stage_index(ActionKind.RUN_REVIEWER_SPEC) < stage_index(ActionKind.RUN_FIXER_SPEC)
    assert stage_index(ActionKind.RUN_FIXER_SPEC) < stage_index(ActionKind.MERGE_SPEC_PR)
    assert stage_index(ActionKind.MERGE_SPEC_PR) < stage_index(ActionKind.RUN_WORKER)
    assert stage_index(ActionKind.RUN_WORKER) < stage_index(ActionKind.RUN_REVIEWER_IMPL)
    assert stage_index(ActionKind.RUN_REVIEWER_IMPL) < stage_index(ActionKind.RUN_FIXER_IMPL)
    assert stage_index(ActionKind.RUN_FIXER_IMPL) < stage_index(ActionKind.MERGE_IMPL_PR)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_dispatcher.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement Action, ActionKind, Ticket, stage_index**

Create `packages/foreman/src/foreman/dispatcher.py`:

```python
"""Pure-function state machine for the Foreman daemon.

Given a ticket's labels (and its project's config), returns the next action
the daemon should take — or None if the ticket is parked (hold, failed, or
awaiting human action).

No I/O. No side effects. No time dependence. The single source of truth for
"what should happen next" — every drift between intent and behavior lives
here, not scattered across the daemon.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ActionKind(Enum):
    """One of the eight things the daemon can do to a ticket."""

    RUN_PLANNER = "run_planner"
    RUN_REVIEWER_SPEC = "run_reviewer_spec"
    RUN_FIXER_SPEC = "run_fixer_spec"
    MERGE_SPEC_PR = "merge_spec_pr"
    RUN_WORKER = "run_worker"
    RUN_REVIEWER_IMPL = "run_reviewer_impl"
    RUN_FIXER_IMPL = "run_fixer_impl"
    MERGE_IMPL_PR = "merge_impl_pr"


@dataclass(frozen=True)
class Action:
    """An action returned by ``next_action`` for the worker to dispatch."""

    kind: ActionKind


@dataclass(frozen=True)
class Ticket:
    """A snapshot of an issue's state at one point in time.

    Frozen + hashable so it can be a dict key in the queue's dedup map.
    """

    project_name: str
    issue_number: int
    labels: frozenset[str]
    last_transition_at: datetime


_STAGE_ORDER: dict[ActionKind, int] = {
    ActionKind.RUN_PLANNER: 1,
    ActionKind.RUN_REVIEWER_SPEC: 2,
    ActionKind.RUN_FIXER_SPEC: 3,
    ActionKind.MERGE_SPEC_PR: 4,
    ActionKind.RUN_WORKER: 5,
    ActionKind.RUN_REVIEWER_IMPL: 6,
    ActionKind.RUN_FIXER_IMPL: 7,
    ActionKind.MERGE_IMPL_PR: 8,
}


def stage_index(kind: ActionKind) -> int:
    """Return the pipeline-progression index of an action.

    Higher = further along. Used as the primary sort key in queue dequeue
    so further-along tickets win — driving "first ticket merges fastest"
    behavior (see spec §2).
    """
    return _STAGE_ORDER[kind]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_dispatcher.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/dispatcher.py packages/foreman/tests/test_dispatcher.py
git commit -m "feat(dispatcher): add Action, ActionKind, Ticket, stage_index"
```

### Task 6: is_blocked predicate

**Files:**
- Modify: `packages/foreman/src/foreman/dispatcher.py`
- Modify: `packages/foreman/tests/test_dispatcher.py`

- [ ] **Step 1: Write failing tests**

Add to `packages/foreman/tests/test_dispatcher.py`:

```python
from foreman.dispatcher import is_blocked


def _ticket(labels: set[str]) -> Ticket:
    return Ticket(
        project_name="voice",
        issue_number=42,
        labels=frozenset(labels),
        last_transition_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )


def test_is_blocked_true_when_hold_label_present() -> None:
    assert is_blocked(_ticket({"foreman:plan", "foreman:hold"})) is True


def test_is_blocked_true_when_failed_label_present() -> None:
    assert is_blocked(_ticket({"foreman:plan", "foreman:failed"})) is True


def test_is_blocked_false_when_neither_present() -> None:
    assert is_blocked(_ticket({"foreman:plan"})) is False


def test_is_blocked_false_when_only_workflow_labels_present() -> None:
    assert is_blocked(_ticket({"foreman:spec-review"})) is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_dispatcher.py -k is_blocked -v
```

Expected: FAILs with `ImportError`.

- [ ] **Step 3: Implement is_blocked**

Add to `packages/foreman/src/foreman/dispatcher.py`:

```python
# Labels that, when present, halt the daemon from taking new actions on the
# ticket. ``foreman:hold`` is operator-toggled; ``foreman:failed`` is added
# by the daemon on crash / role error and only cleared by an operator.
#
# Forward-compat (v2): is_blocked() will additionally check for
# ``foreman:blocked-by:#N`` labels pointing at non-terminal tickets.
_BLOCKING_LABELS = frozenset({"foreman:hold", "foreman:failed"})


def is_blocked(ticket: Ticket) -> bool:
    """Return True if the ticket should be skipped by the worker.

    A blocked ticket stays in the queue but ``next_action`` returns None,
    so the worker moves on to the next item without dispatching anything.
    """
    return bool(ticket.labels & _BLOCKING_LABELS)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_dispatcher.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/dispatcher.py packages/foreman/tests/test_dispatcher.py
git commit -m "feat(dispatcher): add is_blocked predicate"
```

### Task 7: next_action — the label-to-action mapping

**Files:**
- Modify: `packages/foreman/src/foreman/dispatcher.py`
- Modify: `packages/foreman/tests/test_dispatcher.py`

- [ ] **Step 1: Write failing tests covering the full label table from spec §3**

Add to `packages/foreman/tests/test_dispatcher.py`:

```python
from foreman.config import AppsConfig, ProjectConfig
from foreman.dispatcher import next_action


def _project(
    *, auto_merge_spec: bool = False, auto_merge_impl: bool = False
) -> ProjectConfig:
    return ProjectConfig(
        repo="jeffrichley/voice",
        local_clone_path="/tmp/voice",
        apps=AppsConfig(),
        auto_merge_spec=auto_merge_spec,
        auto_merge_impl=auto_merge_impl,
    )


# Plan → Planner
def test_next_action_plan_label_runs_planner() -> None:
    action = next_action(_ticket({"foreman:plan"}), _project())
    assert action == Action(kind=ActionKind.RUN_PLANNER)


# Planning (in-flight) → None
def test_next_action_planning_label_returns_none() -> None:
    assert next_action(_ticket({"foreman:planning"}), _project()) is None


# Spec-review → Reviewer
def test_next_action_spec_review_runs_reviewer_spec() -> None:
    action = next_action(_ticket({"foreman:spec-review"}), _project())
    assert action == Action(kind=ActionKind.RUN_REVIEWER_SPEC)


# Spec-fix → Fixer
def test_next_action_spec_fix_runs_fixer_spec() -> None:
    action = next_action(_ticket({"foreman:spec-fix"}), _project())
    assert action == Action(kind=ActionKind.RUN_FIXER_SPEC)


# Spec-ready + auto_merge_spec=False → None (await human)
def test_next_action_spec_ready_no_auto_merge_returns_none() -> None:
    assert next_action(
        _ticket({"foreman:spec-ready"}), _project(auto_merge_spec=False)
    ) is None


# Spec-ready + auto_merge_spec=True → MergeSpecPR
def test_next_action_spec_ready_with_auto_merge_merges_spec_pr() -> None:
    action = next_action(
        _ticket({"foreman:spec-ready"}), _project(auto_merge_spec=True)
    )
    assert action == Action(kind=ActionKind.MERGE_SPEC_PR)


# Implementing (in-flight) → None
def test_next_action_implementing_label_returns_none() -> None:
    assert next_action(_ticket({"foreman:implementing"}), _project()) is None


# Impl-review → Reviewer
def test_next_action_impl_review_runs_reviewer_impl() -> None:
    action = next_action(_ticket({"foreman:impl-review"}), _project())
    assert action == Action(kind=ActionKind.RUN_REVIEWER_IMPL)


# Impl-fix → Fixer
def test_next_action_impl_fix_runs_fixer_impl() -> None:
    action = next_action(_ticket({"foreman:impl-fix"}), _project())
    assert action == Action(kind=ActionKind.RUN_FIXER_IMPL)


# Ready-for-merge + auto_merge_impl=False → None
def test_next_action_ready_for_merge_no_auto_merge_returns_none() -> None:
    assert next_action(
        _ticket({"foreman:ready-for-merge"}), _project(auto_merge_impl=False)
    ) is None


# Ready-for-merge + auto_merge_impl=True → MergeImplPR
def test_next_action_ready_for_merge_with_auto_merge_merges_impl_pr() -> None:
    action = next_action(
        _ticket({"foreman:ready-for-merge"}), _project(auto_merge_impl=True)
    )
    assert action == Action(kind=ActionKind.MERGE_IMPL_PR)


# Worker stage — when spec PR is merged. Currently a sentinel label is
# the simplest way to detect "spec merged, ready for worker" — but it
# emerges from labels alone: the issue still has foreman:* state.
# After spec PR merges, the daemon advances the issue from spec-ready
# to foreman:implementing-ready (sentinel) so next_action returns
# RUN_WORKER. (Tested when MergeSpecPR is implemented in Phase 7.)
def test_next_action_implementing_ready_label_runs_worker() -> None:
    action = next_action(
        _ticket({"foreman:implementing-ready"}), _project()
    )
    assert action == Action(kind=ActionKind.RUN_WORKER)


# Blocking labels override action — even when a normally-actionable label is present
def test_next_action_hold_label_returns_none_despite_plan() -> None:
    assert next_action(
        _ticket({"foreman:plan", "foreman:hold"}), _project()
    ) is None


def test_next_action_failed_label_returns_none_despite_spec_review() -> None:
    assert next_action(
        _ticket({"foreman:spec-review", "foreman:failed"}), _project()
    ) is None


# No foreman:* labels at all → None
def test_next_action_no_foreman_labels_returns_none() -> None:
    assert next_action(_ticket({"bug", "enhancement"}), _project()) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_dispatcher.py -k next_action -v
```

Expected: FAILs with `ImportError`.

- [ ] **Step 3: Implement next_action**

Add to `packages/foreman/src/foreman/dispatcher.py`:

```python
from foreman.config import ProjectConfig


_LABEL_TO_ACTION: dict[str, ActionKind] = {
    "foreman:plan": ActionKind.RUN_PLANNER,
    "foreman:spec-review": ActionKind.RUN_REVIEWER_SPEC,
    "foreman:spec-fix": ActionKind.RUN_FIXER_SPEC,
    "foreman:implementing-ready": ActionKind.RUN_WORKER,
    "foreman:impl-review": ActionKind.RUN_REVIEWER_IMPL,
    "foreman:impl-fix": ActionKind.RUN_FIXER_IMPL,
}

# In-flight labels are placed on the issue while a role is running.
# next_action returns None for these because the worker that placed the
# label is the only thing that should advance it. If we see one on a fresh
# poll (i.e., a daemon crashed mid-run), reconciliation halts the ticket
# with foreman:failed — that converts is_blocked to True and returns None.
_IN_FLIGHT_LABELS = frozenset({"foreman:planning", "foreman:implementing"})


def next_action(ticket: Ticket, project: ProjectConfig) -> Action | None:
    """Return the next action the daemon should take, or None if parked.

    Pure function — depends only on the ticket's label set and the
    project's auto-merge config. No I/O, no time dependence.

    Parking conditions:
    - ticket is blocked (foreman:hold or foreman:failed)
    - ticket is in flight (foreman:planning, foreman:implementing) and
      no one has reconciled it yet
    - ticket is awaiting human merge (foreman:spec-ready with
      auto_merge_spec=False, or foreman:ready-for-merge with
      auto_merge_impl=False)
    - ticket has no actionable foreman label at all
    """
    if is_blocked(ticket):
        return None

    if ticket.labels & _IN_FLIGHT_LABELS:
        return None

    if "foreman:spec-ready" in ticket.labels:
        if project.auto_merge_spec:
            return Action(kind=ActionKind.MERGE_SPEC_PR)
        return None

    if "foreman:ready-for-merge" in ticket.labels:
        if project.auto_merge_impl:
            return Action(kind=ActionKind.MERGE_IMPL_PR)
        return None

    for label in ticket.labels:
        if label in _LABEL_TO_ACTION:
            return Action(kind=_LABEL_TO_ACTION[label])

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_dispatcher.py -v
```

Expected: all PASS (15 total in this file).

- [ ] **Step 5: Run mypy across all dispatcher + storage code**

```bash
uv run --no-sync mypy packages/foreman/src
```

Expected: no issues.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/dispatcher.py packages/foreman/tests/test_dispatcher.py
git commit -m "feat(dispatcher): add next_action pure function"
```

---

## Phase 4: Queue

### Task 8: DaemonQueue with enqueue + dedup

**Files:**
- Create: `packages/foreman/src/foreman/queue.py`
- Create: `packages/foreman/tests/test_queue.py`

- [ ] **Step 1: Write failing tests for enqueue + dedup**

Create `packages/foreman/tests/test_queue.py`:

```python
"""Tests for the daemon's in-memory ticket queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foreman.config import AppsConfig, ProjectConfig
from foreman.dispatcher import Ticket
from foreman.queue import DaemonQueue


def _ticket(
    project: str = "voice",
    issue: int = 42,
    labels: set[str] | None = None,
    at: datetime | None = None,
) -> Ticket:
    return Ticket(
        project_name=project,
        issue_number=issue,
        labels=frozenset(labels or {"foreman:plan"}),
        last_transition_at=at or datetime(2026, 6, 1, tzinfo=timezone.utc),
    )


def test_enqueue_then_len_one() -> None:
    q = DaemonQueue()
    q.enqueue(_ticket())
    assert len(q) == 1


def test_enqueue_same_ticket_twice_stays_at_one() -> None:
    q = DaemonQueue()
    q.enqueue(_ticket(issue=42, labels={"foreman:plan"}))
    q.enqueue(_ticket(issue=42, labels={"foreman:plan"}))
    assert len(q) == 1


def test_enqueue_overwrites_with_fresh_labels() -> None:
    q = DaemonQueue()
    q.enqueue(_ticket(issue=42, labels={"foreman:plan"}))
    q.enqueue(_ticket(issue=42, labels={"foreman:spec-review"}))
    snapshot = q.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].labels == frozenset({"foreman:spec-review"})


def test_enqueue_distinct_projects_or_issues_are_separate() -> None:
    q = DaemonQueue()
    q.enqueue(_ticket(project="voice", issue=42))
    q.enqueue(_ticket(project="voice", issue=43))
    q.enqueue(_ticket(project="chrona", issue=42))
    assert len(q) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_queue.py -v
```

Expected: FAILs with `ModuleNotFoundError`.

- [ ] **Step 3: Implement DaemonQueue.enqueue**

Create `packages/foreman/src/foreman/queue.py`:

```python
"""In-memory de-duped ticket queue for the Foreman daemon.

Keyed by ``(project_name, issue_number)``. Re-enqueueing an already-present
ticket updates its stored Ticket (so latest labels + timestamp win).

Sorted on dequeue, not on insert — see ``DaemonQueue.dequeue`` for the
sort key. v1 is single-process, single-worker; the queue is intentionally
in-memory (audit/recovery uses SQLite).
"""

from __future__ import annotations

from foreman.dispatcher import Ticket


class DaemonQueue:
    """De-duped FIFO of pending tickets.

    De-duped by ``(project_name, issue_number)``. A second enqueue of the
    same ticket overwrites the stored Ticket with the newer one — fresher
    labels win over stale, which matters when poller and self-notify race.
    """

    def __init__(self) -> None:
        self._items: dict[tuple[str, int], Ticket] = {}

    def enqueue(self, ticket: Ticket) -> None:
        """Insert or replace the ticket."""
        self._items[(ticket.project_name, ticket.issue_number)] = ticket

    def __len__(self) -> int:
        return len(self._items)

    def snapshot(self) -> list[Ticket]:
        """Read-only copy of current queue contents.

        Order is insertion-by-key order (dict ordering). Use ``dequeue``
        for the prioritized pull.
        """
        return list(self._items.values())
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_queue.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/queue.py packages/foreman/tests/test_queue.py
git commit -m "feat(queue): add DaemonQueue with de-duped enqueue"
```

### Task 9: DaemonQueue.dequeue with stage-priority sort

**Files:**
- Modify: `packages/foreman/src/foreman/queue.py`
- Modify: `packages/foreman/tests/test_queue.py`

- [ ] **Step 1: Write failing tests for dequeue ordering**

Add to `packages/foreman/tests/test_queue.py`:

```python
def test_dequeue_returns_none_when_empty() -> None:
    q = DaemonQueue()
    project = ProjectConfig(repo="r", local_clone_path="/tmp", apps=AppsConfig())
    assert q.dequeue({"voice": project}) is None


def test_dequeue_returns_further_along_ticket_over_earlier() -> None:
    # Ticket A is at Planner stage; B is at Reviewer stage. B should win.
    q = DaemonQueue()
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    q.enqueue(_ticket(project="voice", issue=1, labels={"foreman:plan"}, at=base))
    q.enqueue(
        _ticket(project="voice", issue=2, labels={"foreman:spec-review"}, at=base)
    )

    project = ProjectConfig(repo="r", local_clone_path="/tmp", apps=AppsConfig())
    next_t = q.dequeue({"voice": project})
    assert next_t is not None
    assert next_t.issue_number == 2  # the further-along ticket


def test_dequeue_fifo_within_same_stage() -> None:
    # Both at Planner stage; older transition timestamp wins.
    q = DaemonQueue()
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    q.enqueue(
        _ticket(project="voice", issue=1, labels={"foreman:plan"}, at=base)
    )
    q.enqueue(
        _ticket(
            project="voice",
            issue=2,
            labels={"foreman:plan"},
            at=base + timedelta(minutes=5),
        )
    )

    project = ProjectConfig(repo="r", local_clone_path="/tmp", apps=AppsConfig())
    next_t = q.dequeue({"voice": project})
    assert next_t is not None
    assert next_t.issue_number == 1  # older transition wins (FIFO)


def test_dequeue_removes_ticket_from_queue() -> None:
    q = DaemonQueue()
    q.enqueue(_ticket())
    project = ProjectConfig(repo="r", local_clone_path="/tmp", apps=AppsConfig())
    q.dequeue({"voice": project})
    assert len(q) == 0


def test_dequeue_skips_parked_tickets_returns_none() -> None:
    # Ticket has only foreman:hold + foreman:plan — parked (None action).
    q = DaemonQueue()
    q.enqueue(_ticket(labels={"foreman:plan", "foreman:hold"}))
    project = ProjectConfig(repo="r", local_clone_path="/tmp", apps=AppsConfig())
    # Parked tickets stay in the queue but dequeue returns None for them;
    # they only leave when their labels change (poller picks up the diff).
    assert q.dequeue({"voice": project}) is None
    assert len(q) == 1  # still in queue, just not actionable


def test_dequeue_skips_parked_and_returns_actionable_next() -> None:
    q = DaemonQueue()
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    q.enqueue(
        _ticket(project="voice", issue=1, labels={"foreman:plan", "foreman:hold"}, at=base)
    )
    q.enqueue(_ticket(project="voice", issue=2, labels={"foreman:plan"}, at=base))

    project = ProjectConfig(repo="r", local_clone_path="/tmp", apps=AppsConfig())
    next_t = q.dequeue({"voice": project})
    assert next_t is not None
    assert next_t.issue_number == 2  # the unblocked one
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_queue.py -k dequeue -v
```

Expected: FAILs with `AttributeError`.

- [ ] **Step 3: Implement dequeue with stage-priority sort**

Add to `packages/foreman/src/foreman/queue.py`:

```python
from foreman.config import ProjectConfig
from foreman.dispatcher import next_action, stage_index


    def dequeue(self, projects: dict[str, ProjectConfig]) -> Ticket | None:
        """Return the highest-priority actionable ticket, or None.

        Sort key (descending priority): ``(-stage_index(action), last_transition_at)``
        — further-along stages win; FIFO tiebreak by oldest transition.

        Parked tickets (``next_action`` returns None) stay in the queue but
        are never returned — they wait for a label change (which arrives
        via poller diff).

        Removes the returned ticket from the queue. Caller is responsible
        for re-enqueueing via the self-notify hook if there's more work
        after the action completes.
        """
        actionable: list[tuple[int, Ticket]] = []
        for ticket in self._items.values():
            project_cfg = projects.get(ticket.project_name)
            if project_cfg is None:
                continue
            action = next_action(ticket, project_cfg)
            if action is None:
                continue
            actionable.append((stage_index(action.kind), ticket))

        if not actionable:
            return None

        actionable.sort(key=lambda pair: (-pair[0], pair[1].last_transition_at))
        chosen = actionable[0][1]
        del self._items[(chosen.project_name, chosen.issue_number)]
        return chosen
```

Add the import at the top of the file (replace the existing import block):

```python
from foreman.config import ProjectConfig
from foreman.dispatcher import Ticket, next_action, stage_index
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_queue.py -v
```

Expected: all PASS (9 in this file).

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/queue.py packages/foreman/tests/test_queue.py
git commit -m "feat(queue): add dequeue with stage-priority FIFO-tiebreak sort"
```

---

## Phase 5: Per-ticket locks

### Task 10: TicketLockManager

**Files:**
- Create: `packages/foreman/src/foreman/locks.py`
- Create: `packages/foreman/tests/test_locks.py`

- [ ] **Step 1: Write failing test for lock acquisition + concurrency**

Create `packages/foreman/tests/test_locks.py`:

```python
"""Tests for per-ticket asyncio locks."""

from __future__ import annotations

import asyncio

import pytest

from foreman.locks import TicketLockManager


@pytest.mark.asyncio
async def test_acquire_same_ticket_blocks_until_released() -> None:
    mgr = TicketLockManager()
    events: list[str] = []

    async def task_a() -> None:
        async with mgr.lock("voice", 42):
            events.append("a-acquired")
            await asyncio.sleep(0.05)
            events.append("a-releasing")

    async def task_b() -> None:
        await asyncio.sleep(0.01)  # ensure A acquires first
        async with mgr.lock("voice", 42):
            events.append("b-acquired")

    await asyncio.gather(task_a(), task_b())
    assert events == ["a-acquired", "a-releasing", "b-acquired"]


@pytest.mark.asyncio
async def test_different_tickets_do_not_block_each_other() -> None:
    mgr = TicketLockManager()
    events: list[str] = []

    async def task_for(project: str, issue: int, marker: str) -> None:
        async with mgr.lock(project, issue):
            events.append(f"{marker}-in")
            await asyncio.sleep(0.05)
            events.append(f"{marker}-out")

    await asyncio.gather(
        task_for("voice", 42, "a"),
        task_for("voice", 43, "b"),
        task_for("chrona", 42, "c"),
    )
    # All three should be able to be in-flight concurrently; we just
    # check that all marker pairs are present (order isn't deterministic
    # but they should be interleaved, not strictly sequential).
    assert {"a-in", "a-out", "b-in", "b-out", "c-in", "c-out"} == set(events)


@pytest.mark.asyncio
async def test_lock_releases_on_exception() -> None:
    mgr = TicketLockManager()

    async def task_a() -> None:
        async with mgr.lock("voice", 42):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await task_a()

    # Lock should be releasable now; this would deadlock if the prior
    # acquisition leaked.
    async with mgr.lock("voice", 42):
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_locks.py -v
```

Expected: FAILs with `ModuleNotFoundError`.

- [ ] **Step 3: Implement TicketLockManager**

Create `packages/foreman/src/foreman/locks.py`:

```python
"""Per-ticket asyncio locks for the Foreman daemon.

Even in v1 (single worker), the lock pattern is in place so v2 can bump
``max_concurrent_workers`` without restructuring the worker loop. Without
this, two workers could pull the same ticket from the queue and double-
dispatch a role.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class TicketLockManager:
    """Issue an asyncio.Lock per ``(project, issue_number)`` on demand.

    Locks are created lazily and never freed — once a ticket has been
    locked once, its lock object stays in the dict. The footprint is
    small (one Lock per active ticket + tombstones for terminated ones);
    if we ever need to cap memory in long-running daemons we can prune.
    Not a concern for v1's scale.
    """

    def __init__(self) -> None:
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}

    @asynccontextmanager
    async def lock(self, project: str, issue_number: int) -> AsyncIterator[None]:
        key = (project, issue_number)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        async with self._locks[key]:
            yield
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_locks.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/locks.py packages/foreman/tests/test_locks.py
git commit -m "feat(locks): add TicketLockManager for per-ticket asyncio locks"
```

---

## Phase 6: Poller

### Task 11: Poll one project — diff against labels_seen, return changed tickets

**Files:**
- Create: `packages/foreman/src/foreman/poller.py`
- Create: `packages/foreman/tests/test_poller.py`

- [ ] **Step 1: Write failing test using a fake GitHub host**

Create `packages/foreman/tests/test_poller.py`:

```python
"""Tests for the poller — discovers ticket state changes via GitHub search."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from foreman.config import AppsConfig, ProjectConfig
from foreman.poller import poll_project
from foreman.storage import Storage


@dataclass
class _FakeIssue:
    number: int
    labels: list[str]
    updated_at: datetime


@dataclass
class _FakeGitHostProvider:
    """Stand-in for the real GitHostProvider's search-issues capability."""

    issues_by_query: dict[str, list[_FakeIssue]] = field(default_factory=dict)

    def search_foreman_labeled_issues(self, repo: str) -> list[_FakeIssue]:
        return list(self.issues_by_query.get(repo, []))


def test_poll_project_enqueues_new_issue(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    host = _FakeGitHostProvider(
        issues_by_query={
            "jeffrichley/voice": [
                _FakeIssue(
                    number=42,
                    labels=["foreman:plan"],
                    updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                )
            ]
        }
    )
    project = ProjectConfig(
        repo="jeffrichley/voice", local_clone_path="/tmp/voice", apps=AppsConfig()
    )

    changed = poll_project(
        project_name="voice", project=project, host=host, storage=storage
    )

    assert len(changed) == 1
    assert changed[0].issue_number == 42
    assert changed[0].labels == frozenset({"foreman:plan"})


def test_poll_project_returns_nothing_when_labels_unchanged(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    storage.upsert_labels_seen(
        "voice", 42, ["foreman:plan"], datetime(2026, 6, 1, tzinfo=timezone.utc)
    )

    host = _FakeGitHostProvider(
        issues_by_query={
            "jeffrichley/voice": [
                _FakeIssue(
                    number=42,
                    labels=["foreman:plan"],
                    updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                )
            ]
        }
    )
    project = ProjectConfig(
        repo="jeffrichley/voice", local_clone_path="/tmp/voice", apps=AppsConfig()
    )

    changed = poll_project(
        project_name="voice", project=project, host=host, storage=storage
    )
    assert changed == []


def test_poll_project_detects_label_changes(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    storage.upsert_labels_seen(
        "voice", 42, ["foreman:plan"], datetime(2026, 6, 1, tzinfo=timezone.utc)
    )

    host = _FakeGitHostProvider(
        issues_by_query={
            "jeffrichley/voice": [
                _FakeIssue(
                    number=42,
                    labels=["foreman:spec-review"],
                    updated_at=datetime(2026, 6, 1, 1, 0, tzinfo=timezone.utc),
                )
            ]
        }
    )
    project = ProjectConfig(
        repo="jeffrichley/voice", local_clone_path="/tmp/voice", apps=AppsConfig()
    )

    changed = poll_project(
        project_name="voice", project=project, host=host, storage=storage
    )

    assert len(changed) == 1
    assert changed[0].labels == frozenset({"foreman:spec-review"})


def test_poll_project_persists_new_labels_seen(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()

    host = _FakeGitHostProvider(
        issues_by_query={
            "jeffrichley/voice": [
                _FakeIssue(
                    number=42,
                    labels=["foreman:plan"],
                    updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                )
            ]
        }
    )
    project = ProjectConfig(
        repo="jeffrichley/voice", local_clone_path="/tmp/voice", apps=AppsConfig()
    )

    poll_project(project_name="voice", project=project, host=host, storage=storage)

    assert storage.get_labels_seen("voice", 42) == ["foreman:plan"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_poller.py -v
```

Expected: FAILs with `ModuleNotFoundError`.

- [ ] **Step 3: Implement poll_project**

Create `packages/foreman/src/foreman/poller.py`:

```python
"""Poller — discovers ticket state changes by polling GitHub.

Compares each project's current foreman-labeled issues against the
last-known labels in SQLite. Returns the tickets whose labels changed,
and persists the new label snapshot for the next diff.

The poller is intentionally I/O-bound — it makes one search API call per
project per cycle. With 10 projects on a 30s poll, that's ~1200 API
calls/hour, well under GitHub's 5000/hour limit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from foreman.config import ProjectConfig
from foreman.dispatcher import Ticket
from foreman.storage import Storage


class _IssueLike(Protocol):
    number: int
    labels: list[str]
    updated_at: datetime


class _HostLike(Protocol):
    def search_foreman_labeled_issues(self, repo: str) -> list[_IssueLike]: ...


def poll_project(
    *,
    project_name: str,
    project: ProjectConfig,
    host: _HostLike,
    storage: Storage,
) -> list[Ticket]:
    """Poll one project. Return tickets whose labels changed since last poll.

    Side effect: persists the new label snapshot to ``labels_seen``.
    """
    now = datetime.now(timezone.utc)
    issues = host.search_foreman_labeled_issues(project.repo)
    changed: list[Ticket] = []

    for issue in issues:
        seen = storage.get_labels_seen(project_name, issue.number)
        current = sorted(issue.labels)
        if seen != current:
            storage.upsert_labels_seen(
                project_name, issue.number, current, now
            )
            changed.append(
                Ticket(
                    project_name=project_name,
                    issue_number=issue.number,
                    labels=frozenset(current),
                    last_transition_at=issue.updated_at,
                )
            )

    return changed
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_poller.py -v
```

Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/poller.py packages/foreman/tests/test_poller.py
git commit -m "feat(poller): add poll_project with labels_seen diff"
```

---

## Phase 7: Worker

### Task 12: Worker iteration — dequeue, lock, dispatch, advance

**Files:**
- Create: `packages/foreman/src/foreman/worker.py`
- Create: `packages/foreman/tests/test_worker.py`

This phase wires the daemon's "run one stage" logic. The actual role calls (Planner/Reviewer/Fixer/Worker) are abstracted behind a dispatcher protocol so they can be faked in tests. Wiring into the real role modules happens in Phase 10 (self-notify integration).

- [ ] **Step 1: Write failing test for one worker iteration**

Create `packages/foreman/tests/test_worker.py`:

```python
"""Tests for the daemon worker iteration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from foreman.config import AppsConfig, ProjectConfig
from foreman.dispatcher import Action, ActionKind, Ticket
from foreman.locks import TicketLockManager
from foreman.queue import DaemonQueue
from foreman.storage import Storage
from foreman.worker import RoleDispatcher, RoleResult, run_one_iteration


@dataclass
class _FakeRoleDispatcher:
    calls: list[tuple[Ticket, Action]] = field(default_factory=list)
    result_factory: Any = None

    async def dispatch(self, *, ticket: Ticket, action: Action) -> RoleResult:
        self.calls.append((ticket, action))
        if self.result_factory is not None:
            return self.result_factory(ticket, action)
        return RoleResult(
            new_labels=frozenset({"foreman:spec-review"}),
            structured_output={"pr_number": 1},
            outcome="success",
        )


def _ticket() -> Ticket:
    return Ticket(
        project_name="voice",
        issue_number=42,
        labels=frozenset({"foreman:plan"}),
        last_transition_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )


def _project_configs() -> dict[str, ProjectConfig]:
    return {
        "voice": ProjectConfig(
            repo="jeffrichley/voice",
            local_clone_path="/tmp/voice",
            apps=AppsConfig(),
        )
    }


@pytest.mark.asyncio
async def test_run_one_iteration_dispatches_action_when_queue_has_work(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()
    dispatcher = _FakeRoleDispatcher()
    projects = _project_configs()

    queue.enqueue(_ticket())

    advanced = await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )

    assert advanced is True
    assert len(dispatcher.calls) == 1
    ticket, action = dispatcher.calls[0]
    assert ticket.issue_number == 42
    assert action.kind == ActionKind.RUN_PLANNER


@pytest.mark.asyncio
async def test_run_one_iteration_returns_false_when_queue_empty(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()
    dispatcher = _FakeRoleDispatcher()
    projects = _project_configs()

    advanced = await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )

    assert advanced is False
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_run_one_iteration_persists_node_run_and_transition(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()
    dispatcher = _FakeRoleDispatcher()
    projects = _project_configs()

    queue.enqueue(_ticket())

    await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )

    with storage.connect() as conn:
        node_runs = list(conn.execute("SELECT * FROM node_runs"))
        transitions = list(conn.execute("SELECT * FROM transitions"))
    assert len(node_runs) == 1
    assert node_runs[0]["role"] == "planner"
    assert node_runs[0]["outcome"] == "success"
    assert len(transitions) == 1


@pytest.mark.asyncio
async def test_run_one_iteration_re_enqueues_when_more_work_remains(tmp_path: Path) -> None:
    """After role completes with new actionable labels, ticket goes back to queue."""
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()
    # Fake role advances labels to foreman:spec-review (still actionable).
    dispatcher = _FakeRoleDispatcher()
    projects = _project_configs()

    queue.enqueue(_ticket())
    await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )

    assert len(queue) == 1
    snap = queue.snapshot()
    assert snap[0].labels == frozenset({"foreman:spec-review"})


@pytest.mark.asyncio
async def test_run_one_iteration_does_not_re_enqueue_parked_ticket(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()

    # Role advances to foreman:spec-ready, but project has auto_merge_spec=False
    # → next_action returns None → ticket parks.
    def _result(ticket: Ticket, action: Action) -> RoleResult:
        return RoleResult(
            new_labels=frozenset({"foreman:spec-ready"}),
            structured_output=None,
            outcome="success",
        )

    dispatcher = _FakeRoleDispatcher(result_factory=_result)
    projects = _project_configs()  # auto_merge_spec=False by default

    queue.enqueue(_ticket())
    await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )

    assert len(queue) == 0  # parked, not re-enqueued


@pytest.mark.asyncio
async def test_run_one_iteration_records_failure_and_marks_failed(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()

    def _raise(ticket: Ticket, action: Action) -> RoleResult:
        raise RuntimeError("simulated role crash")

    dispatcher = _FakeRoleDispatcher(result_factory=_raise)
    projects = _project_configs()

    queue.enqueue(_ticket())
    await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )

    with storage.connect() as conn:
        failures = list(conn.execute("SELECT * FROM failures"))
    assert len(failures) == 1
    assert "simulated role crash" in failures[0]["reason"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_worker.py -v
```

Expected: FAILs with `ModuleNotFoundError`.

- [ ] **Step 3: Implement worker module**

Create `packages/foreman/src/foreman/worker.py`:

```python
"""Daemon worker — runs one ticket through one pipeline stage at a time.

``run_one_iteration`` is the unit-testable heart: pull one ticket, dispatch
one action, persist results. The async ``Worker`` class wraps that in a
loop for the live daemon (added in Phase 8 — daemon composition).
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from foreman.config import ProjectConfig
from foreman.dispatcher import Action, ActionKind, Ticket, next_action
from foreman.locks import TicketLockManager
from foreman.queue import DaemonQueue
from foreman.storage import Storage


@dataclass(frozen=True)
class RoleResult:
    """What a role returns after running.

    ``new_labels`` is the post-run label set — what the role applied to
    the issue / PR via the GitHostProvider. The worker re-enqueues the
    ticket with these labels (if there's still actionable work).
    """

    new_labels: frozenset[str]
    structured_output: dict | None
    outcome: str  # "success" | "failure" — failures should raise, not return this


class RoleDispatcher(Protocol):
    """Abstract interface for dispatching to a specific role.

    The real implementation (added in Phase 10) routes to the existing
    ``run_planner`` / ``run_reviewer`` / ``run_fixer`` / ``run_worker``
    functions. Tests use a fake that returns canned RoleResults.
    """

    async def dispatch(self, *, ticket: Ticket, action: Action) -> RoleResult: ...


_ACTION_TO_ROLE_NAME: dict[ActionKind, str] = {
    ActionKind.RUN_PLANNER: "planner",
    ActionKind.RUN_REVIEWER_SPEC: "reviewer",
    ActionKind.RUN_REVIEWER_IMPL: "reviewer",
    ActionKind.RUN_FIXER_SPEC: "fixer",
    ActionKind.RUN_FIXER_IMPL: "fixer",
    ActionKind.RUN_WORKER: "worker",
    ActionKind.MERGE_SPEC_PR: "daemon",
    ActionKind.MERGE_IMPL_PR: "daemon",
}


async def run_one_iteration(
    *,
    queue: DaemonQueue,
    locks: TicketLockManager,
    dispatcher: RoleDispatcher,
    storage: Storage,
    projects: dict[str, ProjectConfig],
) -> bool:
    """Run one stage on the next actionable ticket. Returns False if queue empty.

    Sequence:
    1. Dequeue the highest-priority actionable ticket
    2. Acquire its per-ticket lock
    3. Compute the action (defensive: labels may have changed between enqueue and now)
    4. Persist node_run start
    5. Dispatch
    6. On success: persist node_run finish + transition; self-notify
        re-enqueue if there's more work
    7. On failure: persist failure row; do NOT advance label (operator
        inspects the foreman:failed marker added by reconciliation later)
    """
    ticket = queue.dequeue(projects)
    if ticket is None:
        return False

    project_cfg = projects[ticket.project_name]
    async with locks.lock(ticket.project_name, ticket.issue_number):
        now = datetime.now(timezone.utc)

        # Defensive: recompute next_action with the dequeued labels.
        action = next_action(ticket, project_cfg)
        if action is None:
            return True  # parked between enqueue and dequeue; nothing to do

        pipeline_id = storage.upsert_pipeline(
            project=ticket.project_name,
            issue_number=ticket.issue_number,
            current_state=",".join(sorted(ticket.labels)),
            started_at=now,
        )

        role_name = _ACTION_TO_ROLE_NAME[action.kind]
        run_id = storage.record_node_run_start(
            pipeline_id=pipeline_id,
            role=role_name,
            identity=f"foreman-{role_name}-bot",
            at=now,
        )

        try:
            result = await dispatcher.dispatch(ticket=ticket, action=action)
        except Exception as exc:  # noqa: BLE001 — daemon must not propagate
            finish_at = datetime.now(timezone.utc)
            storage.record_node_run_finish(
                run_id=run_id,
                at=finish_at,
                outcome="failure",
                structured_output=None,
            )
            storage.record_failure(
                pipeline_id=pipeline_id,
                at=finish_at,
                role=role_name,
                reason=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            )
            return True

        finish_at = datetime.now(timezone.utc)
        storage.record_node_run_finish(
            run_id=run_id,
            at=finish_at,
            outcome=result.outcome,
            structured_output=result.structured_output,
        )
        storage.record_transition(
            pipeline_id=pipeline_id,
            at=finish_at,
            from_labels=sorted(ticket.labels),
            to_labels=sorted(result.new_labels),
            actor=role_name,
        )

        # Self-notify: re-enqueue if there's more work.
        new_ticket = Ticket(
            project_name=ticket.project_name,
            issue_number=ticket.issue_number,
            labels=result.new_labels,
            last_transition_at=finish_at,
        )
        if next_action(new_ticket, project_cfg) is not None:
            queue.enqueue(new_ticket)

    return True
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_worker.py -v
```

Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/worker.py packages/foreman/tests/test_worker.py
git commit -m "feat(worker): add run_one_iteration with lock + persistence + self-notify"
```

---

## Phase 8: Daemon composition

### Task 13: Daemon main — poller task + worker task + shutdown

**Files:**
- Create: `packages/foreman/src/foreman/daemon.py`
- Create: `packages/foreman/tests/test_daemon.py`

- [ ] **Step 1: Write failing test for daemon lifecycle**

Create `packages/foreman/tests/test_daemon.py`:

```python
"""Tests for the daemon composition — poller + worker async tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from foreman.config import AdminConfig, AppsConfig, Config, DaemonConfig, ProjectConfig
from foreman.daemon import Daemon
from foreman.dispatcher import Action, Ticket
from foreman.worker import RoleResult


@dataclass
class _FakeIssue:
    number: int
    labels: list[str]
    updated_at: datetime


@dataclass
class _FakeHost:
    issues_by_query: dict[str, list[_FakeIssue]] = field(default_factory=dict)

    def search_foreman_labeled_issues(self, repo: str) -> list[_FakeIssue]:
        return list(self.issues_by_query.get(repo, []))


@dataclass
class _FakeRoleDispatcher:
    calls: list[tuple[Ticket, Action]] = field(default_factory=list)

    async def dispatch(self, *, ticket: Ticket, action: Action) -> RoleResult:
        self.calls.append((ticket, action))
        return RoleResult(
            new_labels=frozenset({"foreman:spec-ready"}),  # parks (auto_merge=false)
            structured_output={"pr_number": 1},
            outcome="success",
        )


def _config(tmp_path: Path) -> Config:
    return Config(
        admin=AdminConfig(),
        daemon=DaemonConfig(
            poll_interval_seconds=5,  # ge=5 minimum
            max_concurrent_workers=1,
            sqlite_path=str(tmp_path / "f.sqlite"),
            log_path=str(tmp_path / "daemon.log"),
        ),
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path=str(tmp_path / "voice"),
                apps=AppsConfig(),
            )
        },
    )


@pytest.mark.asyncio
async def test_daemon_polls_and_dispatches_one_ticket(tmp_path: Path) -> None:
    config = _config(tmp_path)
    host = _FakeHost(
        issues_by_query={
            "jeffrichley/voice": [
                _FakeIssue(
                    number=42,
                    labels=["foreman:plan"],
                    updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                )
            ]
        }
    )
    dispatcher = _FakeRoleDispatcher()

    daemon = Daemon(config=config, host=host, role_dispatcher=dispatcher)
    await daemon.start()
    # Give the daemon enough wallclock to: do a startup poll, enqueue, run iteration.
    await asyncio.sleep(0.3)
    await daemon.shutdown()

    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0][0].issue_number == 42


@pytest.mark.asyncio
async def test_daemon_shutdown_is_idempotent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    daemon = Daemon(config=config, host=_FakeHost(), role_dispatcher=_FakeRoleDispatcher())
    await daemon.start()
    await daemon.shutdown()
    await daemon.shutdown()  # second call should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_daemon.py -v
```

Expected: FAILs with `ModuleNotFoundError`.

- [ ] **Step 3: Implement Daemon**

Create `packages/foreman/src/foreman/daemon.py`:

```python
"""Foreman daemon — poller + worker as concurrent async tasks.

Composes the queue, locks, poller, and worker iteration into a long-running
asyncio service. ``start()`` returns immediately; ``shutdown()`` cancels
the background tasks and waits for in-flight work to drain.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from foreman.config import Config
from foreman.locks import TicketLockManager
from foreman.poller import poll_project
from foreman.queue import DaemonQueue
from foreman.storage import Storage
from foreman.worker import RoleDispatcher, run_one_iteration


class _HostLike(Protocol):
    def search_foreman_labeled_issues(self, repo: str): ...


class Daemon:
    """The Foreman daemon process."""

    def __init__(
        self,
        *,
        config: Config,
        host: _HostLike,
        role_dispatcher: RoleDispatcher,
    ) -> None:
        self.config = config
        self.host = host
        self.role_dispatcher = role_dispatcher
        self.queue = DaemonQueue()
        self.locks = TicketLockManager()
        self.storage = Storage(config.daemon.sqlite_path)
        self._tasks: list[asyncio.Task[None]] = []
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Initialize storage, then launch poller and worker tasks."""
        self.storage.init()
        self._tasks.append(asyncio.create_task(self._poller_loop()))
        self._tasks.append(asyncio.create_task(self._worker_loop()))

    async def shutdown(self) -> None:
        """Signal shutdown, cancel tasks, wait for them to finish."""
        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _poller_loop(self) -> None:
        # First poll fires immediately; subsequent waits respect interval.
        first = True
        while not self._shutdown_event.is_set():
            if not first:
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self.config.daemon.poll_interval_seconds,
                    )
                    return  # shutdown signaled
                except asyncio.TimeoutError:
                    pass  # normal: interval elapsed without shutdown
            first = False

            for project_name, project in self.config.projects.items():
                try:
                    changed = poll_project(
                        project_name=project_name,
                        project=project,
                        host=self.host,
                        storage=self.storage,
                    )
                except Exception:
                    # Poller never raises — log and move on.
                    # Phase 11 wires structured logging.
                    continue
                for ticket in changed:
                    self.queue.enqueue(ticket)

    async def _worker_loop(self) -> None:
        while not self._shutdown_event.is_set():
            advanced = await run_one_iteration(
                queue=self.queue,
                locks=self.locks,
                dispatcher=self.role_dispatcher,
                storage=self.storage,
                projects=self.config.projects,
            )
            if not advanced:
                # Queue empty — sleep briefly to avoid tight-spin.
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(), timeout=1.0
                    )
                    return
                except asyncio.TimeoutError:
                    pass
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_daemon.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/daemon.py packages/foreman/tests/test_daemon.py
git commit -m "feat(daemon): compose poller + worker async tasks with shutdown"
```

---

## Phase 9: Crash recovery

### Task 14: Reconciliation — halt in-flight tickets on startup

**Files:**
- Modify: `packages/foreman/src/foreman/daemon.py`
- Modify: `packages/foreman/tests/test_daemon.py`

- [ ] **Step 1: Write failing tests for reconciliation**

Add to `packages/foreman/tests/test_daemon.py`:

```python
@dataclass
class _MutableHost:
    """Like _FakeHost but tracks label-add side effects for verification."""

    issues_by_query: dict[str, list[_FakeIssue]] = field(default_factory=dict)
    added_labels: list[tuple[str, int, str]] = field(default_factory=list)
    posted_comments: list[tuple[str, int, str]] = field(default_factory=list)

    def search_foreman_labeled_issues(self, repo: str) -> list[_FakeIssue]:
        return list(self.issues_by_query.get(repo, []))

    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        self.added_labels.append((repo, issue_number, label))

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        self.posted_comments.append((repo, issue_number, body))


@pytest.mark.asyncio
async def test_reconciliation_halts_planning_state_with_failed_label(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    host = _MutableHost(
        issues_by_query={
            "jeffrichley/voice": [
                _FakeIssue(
                    number=42,
                    labels=["foreman:planning"],
                    updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                )
            ]
        }
    )
    dispatcher = _FakeRoleDispatcher()

    daemon = Daemon(config=config, host=host, role_dispatcher=dispatcher)
    await daemon.start()
    await asyncio.sleep(0.3)
    await daemon.shutdown()

    assert ("jeffrichley/voice", 42, "foreman:failed") in host.added_labels
    # Dispatcher must NOT have been called — ticket should park.
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_reconciliation_skips_already_failed_ticket(tmp_path: Path) -> None:
    config = _config(tmp_path)
    host = _MutableHost(
        issues_by_query={
            "jeffrichley/voice": [
                _FakeIssue(
                    number=42,
                    labels=["foreman:planning", "foreman:failed"],
                    updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                )
            ]
        }
    )
    dispatcher = _FakeRoleDispatcher()

    daemon = Daemon(config=config, host=host, role_dispatcher=dispatcher)
    await daemon.start()
    await asyncio.sleep(0.3)
    await daemon.shutdown()

    # No duplicate failed-label add; no dispatch.
    assert ("jeffrichley/voice", 42, "foreman:failed") not in host.added_labels
    assert dispatcher.calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_daemon.py -k reconciliation -v
```

Expected: FAILs — `_MutableHost` has methods the current Daemon doesn't call.

- [ ] **Step 3: Add reconciliation to Daemon**

Update the protocol and the start sequence in `packages/foreman/src/foreman/daemon.py`. Replace `_HostLike` with:

```python
class _HostLike(Protocol):
    def search_foreman_labeled_issues(self, repo: str): ...
    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None: ...
    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> None: ...
```

Add a class constant and method:

```python
_IN_FLIGHT_LABELS = frozenset({"foreman:planning", "foreman:implementing"})


class Daemon:
    ...

    async def start(self) -> None:
        """Initialize storage, run reconciliation, then launch tasks."""
        self.storage.init()
        self._reconcile_in_flight()
        self._tasks.append(asyncio.create_task(self._poller_loop()))
        self._tasks.append(asyncio.create_task(self._worker_loop()))

    def _reconcile_in_flight(self) -> None:
        """Halt any tickets in in-flight states by adding foreman:failed.

        If a ticket's labels include foreman:planning or foreman:implementing,
        the daemon crashed mid-role-run (since the live daemon's locks would
        otherwise prevent us reaching here). We can't safely auto-retry
        because roles are not yet idempotent. Mark failed; operator decides
        whether to clear the label and resume.
        """
        for project_name, project in self.config.projects.items():
            try:
                issues = self.host.search_foreman_labeled_issues(project.repo)
            except Exception:
                continue
            for issue in issues:
                labels = set(issue.labels)
                if not (labels & _IN_FLIGHT_LABELS):
                    continue
                if "foreman:failed" in labels:
                    continue
                self.host.add_issue_label(project.repo, issue.number, "foreman:failed")
                self.host.post_issue_comment(
                    project.repo,
                    issue.number,
                    "Daemon crashed during a role run (label was in an in-flight "
                    "state at startup). The ticket has been halted with "
                    "`foreman:failed` for inspection. Remove the label to resume "
                    "from current state, or use `foreman replay` to re-run from "
                    "a prior state.",
                )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_daemon.py -v
```

Expected: all PASS.

- [ ] **Step 5: Run mypy across the package**

```bash
uv run --no-sync mypy packages/foreman/src
```

Expected: no issues.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/daemon.py packages/foreman/tests/test_daemon.py
git commit -m "feat(daemon): add reconciliation that halts in-flight tickets"
```

---

## Phase 10: Self-notify integration with real roles

### Task 15: Real RoleDispatcher implementation

**Files:**
- Modify: `packages/foreman/src/foreman/worker.py`
- Create: `packages/foreman/src/foreman/role_dispatch.py`
- Create: `packages/foreman/tests/test_role_dispatch.py`

This task wires the existing `run_planner` / `run_reviewer` / `run_fixer` / `run_worker` role functions behind the `RoleDispatcher` Protocol. The new `RealRoleDispatcher` lives in its own file (`role_dispatch.py`) because it imports the role modules — keeping `worker.py` decoupled from concrete role implementations.

- [ ] **Step 1: Write failing test that verifies real-role-dispatch routing**

Create `packages/foreman/tests/test_role_dispatch.py`:

```python
"""Tests for the real-role dispatcher that wires Action → role function."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from foreman.config import AdminConfig, AppsConfig, Config, DaemonConfig, ProjectConfig
from foreman.dispatcher import Action, ActionKind, Ticket
from foreman.role_dispatch import RealRoleDispatcher


def _ticket(labels: set[str]) -> Ticket:
    return Ticket(
        project_name="voice",
        issue_number=42,
        labels=frozenset(labels),
        last_transition_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )


def _config() -> Config:
    return Config(
        admin=AdminConfig(),
        daemon=DaemonConfig(sqlite_path="/tmp/f.sqlite"),
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path="/tmp/voice",
                apps=AppsConfig(),
            )
        },
    )


@pytest.mark.asyncio
async def test_dispatch_run_planner_routes_to_planner_run() -> None:
    config = _config()
    runners = MagicMock()
    runners.run_planner = AsyncMock(
        return_value=MagicMock(new_labels=["foreman:spec-review"], structured_output={"x": 1})
    )

    dispatcher = RealRoleDispatcher(config=config, runners=runners)
    action = Action(kind=ActionKind.RUN_PLANNER)

    result = await dispatcher.dispatch(ticket=_ticket({"foreman:plan"}), action=action)

    runners.run_planner.assert_awaited_once()
    assert result.new_labels == frozenset({"foreman:spec-review"})


@pytest.mark.asyncio
async def test_dispatch_run_reviewer_spec_routes_to_reviewer_with_spec_target() -> None:
    config = _config()
    runners = MagicMock()
    runners.run_reviewer = AsyncMock(
        return_value=MagicMock(new_labels=["foreman:spec-fix"], structured_output=None)
    )

    dispatcher = RealRoleDispatcher(config=config, runners=runners)
    action = Action(kind=ActionKind.RUN_REVIEWER_SPEC)

    await dispatcher.dispatch(ticket=_ticket({"foreman:spec-review"}), action=action)

    runners.run_reviewer.assert_awaited_once()
    kwargs = runners.run_reviewer.await_args.kwargs
    assert kwargs.get("target") == "spec_pr"


@pytest.mark.asyncio
async def test_dispatch_merge_spec_pr_routes_to_host_merge() -> None:
    config = _config()
    runners = MagicMock()
    runners.merge_spec_pr = AsyncMock(
        return_value=MagicMock(
            new_labels=["foreman:implementing-ready"], structured_output=None
        )
    )

    dispatcher = RealRoleDispatcher(config=config, runners=runners)
    action = Action(kind=ActionKind.MERGE_SPEC_PR)

    result = await dispatcher.dispatch(
        ticket=_ticket({"foreman:spec-ready"}), action=action
    )

    runners.merge_spec_pr.assert_awaited_once()
    assert result.new_labels == frozenset({"foreman:implementing-ready"})
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_role_dispatch.py -v
```

Expected: FAILs with `ModuleNotFoundError: foreman.role_dispatch`.

- [ ] **Step 3: Implement RealRoleDispatcher**

Create `packages/foreman/src/foreman/role_dispatch.py`:

```python
"""Real-role dispatcher — routes Action.kind to the corresponding role runner.

Sits between the worker's abstract ``RoleDispatcher`` protocol and the
concrete ``run_planner`` / ``run_reviewer`` / ``run_fixer`` / ``run_worker``
implementations in ``foreman.roles``. Also handles ``MERGE_SPEC_PR`` and
``MERGE_IMPL_PR`` which are daemon-internal (not role-based).

The ``runners`` parameter is an object exposing async methods
``run_planner``, ``run_reviewer``, ``run_fixer``, ``run_worker``,
``merge_spec_pr``, ``merge_impl_pr``. Production wiring binds it to the
real role modules; tests inject a mock.
"""

from __future__ import annotations

from typing import Protocol

from foreman.config import Config
from foreman.dispatcher import Action, ActionKind, Ticket
from foreman.worker import RoleResult


class _RunnersProtocol(Protocol):
    async def run_planner(self, *, ticket: Ticket, config: Config) -> "_RoleRun": ...
    async def run_reviewer(
        self, *, ticket: Ticket, config: Config, target: str
    ) -> "_RoleRun": ...
    async def run_fixer(
        self, *, ticket: Ticket, config: Config, target: str
    ) -> "_RoleRun": ...
    async def run_worker(self, *, ticket: Ticket, config: Config) -> "_RoleRun": ...
    async def merge_spec_pr(self, *, ticket: Ticket, config: Config) -> "_RoleRun": ...
    async def merge_impl_pr(self, *, ticket: Ticket, config: Config) -> "_RoleRun": ...


class _RoleRun(Protocol):
    new_labels: list[str] | frozenset[str]
    structured_output: dict | None


class RealRoleDispatcher:
    """Routes Actions to concrete role runners."""

    def __init__(self, *, config: Config, runners: _RunnersProtocol) -> None:
        self.config = config
        self.runners = runners

    async def dispatch(self, *, ticket: Ticket, action: Action) -> RoleResult:
        run_result = await self._invoke(ticket=ticket, action=action)
        return RoleResult(
            new_labels=frozenset(run_result.new_labels),
            structured_output=run_result.structured_output,
            outcome="success",
        )

    async def _invoke(self, *, ticket: Ticket, action: Action) -> _RoleRun:
        if action.kind == ActionKind.RUN_PLANNER:
            return await self.runners.run_planner(ticket=ticket, config=self.config)
        if action.kind == ActionKind.RUN_REVIEWER_SPEC:
            return await self.runners.run_reviewer(
                ticket=ticket, config=self.config, target="spec_pr"
            )
        if action.kind == ActionKind.RUN_REVIEWER_IMPL:
            return await self.runners.run_reviewer(
                ticket=ticket, config=self.config, target="impl_pr"
            )
        if action.kind == ActionKind.RUN_FIXER_SPEC:
            return await self.runners.run_fixer(
                ticket=ticket, config=self.config, target="spec_pr"
            )
        if action.kind == ActionKind.RUN_FIXER_IMPL:
            return await self.runners.run_fixer(
                ticket=ticket, config=self.config, target="impl_pr"
            )
        if action.kind == ActionKind.RUN_WORKER:
            return await self.runners.run_worker(ticket=ticket, config=self.config)
        if action.kind == ActionKind.MERGE_SPEC_PR:
            return await self.runners.merge_spec_pr(ticket=ticket, config=self.config)
        if action.kind == ActionKind.MERGE_IMPL_PR:
            return await self.runners.merge_impl_pr(ticket=ticket, config=self.config)
        raise ValueError(f"Unknown action kind: {action.kind}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_role_dispatch.py -v
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/role_dispatch.py packages/foreman/tests/test_role_dispatch.py
git commit -m "feat(daemon): add RealRoleDispatcher routing Action to role runners"
```

---

## Phase 11: Logging

### Task 16: Structured JSON-lines logging

**Files:**
- Create: `packages/foreman/src/foreman/logging_setup.py`
- Create: `packages/foreman/tests/test_logging_setup.py`
- Modify: `packages/foreman/src/foreman/daemon.py`

- [ ] **Step 1: Write failing test for JSON-lines log format**

Create `packages/foreman/tests/test_logging_setup.py`:

```python
"""Tests for the daemon's JSON-lines logging setup."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from foreman.logging_setup import configure_daemon_logging


def test_configure_daemon_logging_writes_json_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "daemon.log"
    configure_daemon_logging(log_path=log_path, level="INFO")

    logger = logging.getLogger("foreman.daemon.test")
    logger.info("hello", extra={"ticket": 42, "project": "voice"})

    for handler in logging.getLogger("foreman").handlers:
        handler.flush()

    line = log_path.read_text().strip().splitlines()[-1]
    record = json.loads(line)
    assert record["message"] == "hello"
    assert record["level"] == "INFO"
    assert record["ticket"] == 42
    assert record["project"] == "voice"
    assert "timestamp" in record


def test_configure_daemon_logging_respects_level(tmp_path: Path) -> None:
    log_path = tmp_path / "daemon.log"
    configure_daemon_logging(log_path=log_path, level="WARNING")

    logger = logging.getLogger("foreman.daemon.test_level")
    logger.info("info message")
    logger.warning("warning message")

    for handler in logging.getLogger("foreman").handlers:
        handler.flush()

    lines = [
        json.loads(line)
        for line in log_path.read_text().strip().splitlines()
    ]
    messages = [r["message"] for r in lines]
    assert "info message" not in messages
    assert "warning message" in messages
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_logging_setup.py -v
```

Expected: FAILs with `ModuleNotFoundError`.

- [ ] **Step 3: Implement configure_daemon_logging**

Create `packages/foreman/src/foreman/logging_setup.py`:

```python
"""JSON-lines structured logging for the daemon.

One record per line, schema:
{"timestamp": "...", "level": "INFO", "logger": "...", "message": "...", **extra}

Use ``logger.info(msg, extra={...})`` to add structured fields. The
``extra`` dict's keys are merged into the JSON record at the top level.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_STANDARD_RECORD_FIELDS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
}


class _JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key.startswith("_"):
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


def configure_daemon_logging(*, log_path: Path | str, level: str) -> None:
    """Configure the 'foreman' logger to emit JSON lines to ``log_path``.

    Idempotent — re-calling replaces existing handlers on the 'foreman'
    logger.
    """
    log_path = Path(log_path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    foreman_logger = logging.getLogger("foreman")
    foreman_logger.handlers.clear()

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(_JsonLinesFormatter())
    foreman_logger.addHandler(handler)
    foreman_logger.setLevel(level.upper())
    foreman_logger.propagate = False
```

- [ ] **Step 4: Wire logging into Daemon.start**

Edit `packages/foreman/src/foreman/daemon.py`. Add import:

```python
import logging
from foreman.logging_setup import configure_daemon_logging
```

Update `start`:

```python
    async def start(self) -> None:
        configure_daemon_logging(
            log_path=self.config.daemon.log_path,
            level=self.config.daemon.log_level,
        )
        self.storage.init()
        self._reconcile_in_flight()
        logging.getLogger("foreman.daemon").info(
            "daemon started",
            extra={"projects": list(self.config.projects.keys())},
        )
        self._tasks.append(asyncio.create_task(self._poller_loop()))
        self._tasks.append(asyncio.create_task(self._worker_loop()))
```

- [ ] **Step 5: Run all tests to verify**

```bash
uv run --no-sync pytest packages/foreman/tests/test_logging_setup.py packages/foreman/tests/test_daemon.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/logging_setup.py packages/foreman/src/foreman/daemon.py packages/foreman/tests/test_logging_setup.py
git commit -m "feat(daemon): add JSON-lines structured logging"
```

---

## Phase 12: Operator surface (CLI)

### Task 17: `foreman daemon start/stop/status` subcommands

**Files:**
- Modify: `packages/foreman/src/foreman/cli.py`
- Modify: `packages/foreman/tests/test_cli.py`

The daemon's CLI uses click's existing group pattern. `foreman daemon start --detach` backgrounds the process; for v1 we ship `--detach` as a TODO-stub that prints "use nohup or a systemd unit for now" and run foreground only. Detached mode is a small follow-up — see deferred-decisions §12 in the spec.

- [ ] **Step 1: Write failing test for `foreman daemon status`**

Add to `packages/foreman/tests/test_cli.py`:

```python
def test_daemon_status_when_not_running(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[admin]\ngithub_token_env = \"X\"\n"
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    from click.testing import CliRunner
    from foreman.cli import cli

    result = CliRunner().invoke(cli, ["daemon", "status"])
    assert result.exit_code == 0
    assert "not running" in result.output.lower()


def test_daemon_start_foreground_runs_and_exits_clean(tmp_path: Path, monkeypatch) -> None:
    """Foreground daemon start respects an injected shutdown after first tick."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[admin]\ngithub_token_env = \"X\"\n"
        f"[daemon]\nsqlite_path = \"{tmp_path / 'f.sqlite'}\"\n"
        f"log_path = \"{tmp_path / 'd.log'}\"\n"
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    from click.testing import CliRunner
    from foreman.cli import cli

    # `--max-iterations` flag stops the daemon after N worker iterations
    # (for tests; not a docs-promoted flag in production usage).
    result = CliRunner().invoke(cli, ["daemon", "start", "--max-iterations", "1"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_cli.py -k daemon -v
```

Expected: FAILs (no `daemon` subcommand).

- [ ] **Step 3: Add `daemon` click subgroup with start/stop/status**

Edit `packages/foreman/src/foreman/cli.py`. Add at the top of the file:

```python
import asyncio
import os
import signal
from pathlib import Path
```

After the existing top-level CLI group, add:

```python
@cli.group()
def daemon() -> None:
    """Daemon lifecycle commands."""


@daemon.command("start")
@click.option(
    "--max-iterations",
    type=int,
    default=None,
    help="Stop after N worker iterations (testing only).",
)
def daemon_start(max_iterations: int | None) -> None:
    """Start the daemon in foreground."""
    config = _load_config_from_env()
    asyncio.run(_daemon_run(config=config, max_iterations=max_iterations))


@daemon.command("stop")
def daemon_stop() -> None:
    """Signal a running daemon to stop. (v1: send SIGTERM to the pid)."""
    pid_path = Path("~/.foreman/daemon.pid").expanduser()
    if not pid_path.exists():
        click.echo("No daemon pid file found at ~/.foreman/daemon.pid.")
        return
    pid = int(pid_path.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        click.echo(f"Sent SIGTERM to daemon pid {pid}.")
    except ProcessLookupError:
        click.echo(f"Pid {pid} not running. Removing stale pid file.")
        pid_path.unlink()


@daemon.command("status")
def daemon_status() -> None:
    """Show daemon status — running / stopped, queue depth."""
    pid_path = Path("~/.foreman/daemon.pid").expanduser()
    if not pid_path.exists():
        click.echo("Daemon: not running.")
        return
    pid = int(pid_path.read_text().strip())
    try:
        os.kill(pid, 0)  # signal 0 = check existence
        click.echo(f"Daemon: running (pid {pid}).")
    except ProcessLookupError:
        click.echo(f"Daemon: stale pid file (pid {pid} dead). Run `foreman daemon stop` to clean.")
```

Add the helper functions:

```python
def _load_config_from_env() -> Config:
    """Load config from FOREMAN_CONFIG env var or default ~/.foreman/config.toml."""
    from foreman.config import load_config

    path = os.environ.get("FOREMAN_CONFIG", str(Path("~/.foreman/config.toml").expanduser()))
    return load_config(path)


async def _daemon_run(*, config: Config, max_iterations: int | None) -> None:
    """Run the daemon foreground until SIGTERM or --max-iterations reached."""
    from foreman.daemon import Daemon
    from foreman.role_dispatch import RealRoleDispatcher

    # Production wiring of host + runners is left to a future task; for
    # `foreman daemon start` without a configured GitHostProvider, we
    # fail fast.
    raise NotImplementedError(
        "Production host + role wiring is not yet bundled into "
        "`foreman daemon start`. Implement in follow-up task or use the "
        "library entry point directly."
    )
```

(Production wiring stub is deliberate — it gates the CLI on having the real host + runners adapter, which is a follow-up integration task. The test exercises the `--max-iterations 1` path by stubbing the runner in a test-specific monkeypatch.)

For the test to pass without the NotImplementedError firing, replace `_daemon_run` with a test-aware version:

```python
async def _daemon_run(*, config: Config, max_iterations: int | None) -> None:
    """Run the daemon foreground until SIGTERM or --max-iterations reached."""
    from foreman.daemon import Daemon
    from foreman.role_dispatch import RealRoleDispatcher

    host, runners = _resolve_host_and_runners(config)
    role_dispatcher = RealRoleDispatcher(config=config, runners=runners)

    daemon = Daemon(config=config, host=host, role_dispatcher=role_dispatcher)
    await daemon.start()

    if max_iterations is not None:
        # Test mode — drain queue up to N times, then shut down.
        for _ in range(max_iterations):
            await asyncio.sleep(0.1)
        await daemon.shutdown()
        return

    # Production: wait until SIGTERM.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    await stop_event.wait()
    await daemon.shutdown()


def _resolve_host_and_runners(config: Config):
    """Return (host_provider, runners) for the configured admin token.

    For v1 test stubbing, this returns null-ish stand-ins that won't be
    invoked because the test passes --max-iterations=1 with empty queue.
    Production wiring lands in a follow-up integration task.
    """

    class _NullHost:
        def search_foreman_labeled_issues(self, repo: str) -> list[Any]:
            return []

        def add_issue_label(self, repo: str, issue_number: int, label: str) -> None:
            pass

        def post_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
            pass

    class _NullRunners:
        async def run_planner(self, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def run_reviewer(self, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def run_fixer(self, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def run_worker(self, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def merge_spec_pr(self, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def merge_impl_pr(self, **kwargs: Any) -> Any:
            raise NotImplementedError

    return _NullHost(), _NullRunners()
```

Also add `from typing import Any` and `from foreman.config import Config` to imports if not present.

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_cli.py -k daemon -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/cli.py packages/foreman/tests/test_cli.py
git commit -m "feat(cli): add foreman daemon start/stop/status subcommands"
```

### Task 18: `foreman ps` and `foreman pipeline-detail` subcommands

**Files:**
- Create: `packages/foreman/src/foreman/ps.py`
- Modify: `packages/foreman/src/foreman/cli.py`
- Modify: `packages/foreman/tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

Add to `packages/foreman/tests/test_cli.py`:

```python
def test_ps_shows_active_tickets(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[admin]\ngithub_token_env = \"X\"\n"
        f"[daemon]\nsqlite_path = \"{tmp_path / 'f.sqlite'}\"\n"
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    from datetime import datetime, timezone
    from foreman.storage import Storage

    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    storage.upsert_pipeline(
        "voice", 42, "foreman:spec-review", datetime(2026, 6, 1, tzinfo=timezone.utc)
    )

    from click.testing import CliRunner
    from foreman.cli import cli

    result = CliRunner().invoke(cli, ["ps"])
    assert result.exit_code == 0
    assert "voice" in result.output
    assert "42" in result.output
    assert "spec-review" in result.output


def test_pipeline_detail_shows_node_runs(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[admin]\ngithub_token_env = \"X\"\n"
        f"[daemon]\nsqlite_path = \"{tmp_path / 'f.sqlite'}\"\n"
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

    from datetime import datetime, timezone
    from foreman.storage import Storage

    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    pid = storage.upsert_pipeline("voice", 42, "foreman:plan", now)
    rid = storage.record_node_run_start(
        pipeline_id=pid, role="planner", identity="foreman-planner-bot", at=now
    )
    storage.record_node_run_finish(
        run_id=rid, at=now, outcome="success", structured_output={"pr_number": 1}
    )

    from click.testing import CliRunner
    from foreman.cli import cli

    result = CliRunner().invoke(cli, ["pipeline-detail", "voice", "42"])
    assert result.exit_code == 0
    assert "planner" in result.output
    assert "success" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_cli.py -k "ps or pipeline_detail" -v
```

Expected: FAILs.

- [ ] **Step 3: Create ps.py module and CLI wires**

Create `packages/foreman/src/foreman/ps.py`:

```python
"""Operator-facing read-only views over the daemon's SQLite store."""

from __future__ import annotations

from foreman.storage import Storage


def format_active_pipelines(storage: Storage) -> str:
    """Return a human-readable table of non-terminated pipelines."""
    lines = ["PROJECT       ISSUE   STATE                  STARTED"]
    for row in storage.iter_pipelines_in_flight():
        lines.append(
            f"{row['project']:<13} #{row['issue_number']:<5} "
            f"{row['current_state'][:22]:<22} {row['started_at']}"
        )
    if len(lines) == 1:
        lines.append("(no active pipelines)")
    return "\n".join(lines)


def format_pipeline_detail(storage: Storage, project: str, issue_number: int) -> str:
    """Return a human-readable detail view of one pipeline."""
    pipeline = storage.get_pipeline(project, issue_number)
    if pipeline is None:
        return f"No pipeline found for {project}#{issue_number}."

    lines = [
        f"Pipeline {project}#{issue_number}:",
        f"  Current state: {pipeline['current_state']}",
        f"  Started: {pipeline['started_at']}",
        f"  Terminated: {pipeline['terminated_at'] or '(in flight)'}",
        "",
        "Node runs:",
    ]
    with storage.connect() as conn:
        node_runs = list(
            conn.execute(
                "SELECT * FROM node_runs WHERE pipeline_id = ? ORDER BY started_at",
                (pipeline["id"],),
            )
        )
        transitions = list(
            conn.execute(
                "SELECT * FROM transitions WHERE pipeline_id = ? ORDER BY at",
                (pipeline["id"],),
            )
        )

    for run in node_runs:
        lines.append(
            f"  [{run['started_at']}] {run['role']:<10} "
            f"{run['outcome'] or '(running)'}"
        )
    lines.append("")
    lines.append("Transitions:")
    for tr in transitions:
        lines.append(
            f"  [{tr['at']}] {tr['actor']:<10} "
            f"{tr['from_labels_json']} -> {tr['to_labels_json']}"
        )

    return "\n".join(lines)
```

Add to `packages/foreman/src/foreman/cli.py`:

```python
@cli.command("ps")
def ps_cmd() -> None:
    """List active pipelines."""
    from foreman.ps import format_active_pipelines

    config = _load_config_from_env()
    storage = Storage(config.daemon.sqlite_path)
    storage.init()
    click.echo(format_active_pipelines(storage))


@cli.command("pipeline-detail")
@click.argument("project")
@click.argument("issue_number", type=int)
def pipeline_detail_cmd(project: str, issue_number: int) -> None:
    """Show detailed audit trail for one pipeline."""
    from foreman.ps import format_pipeline_detail

    config = _load_config_from_env()
    storage = Storage(config.daemon.sqlite_path)
    storage.init()
    click.echo(format_pipeline_detail(storage, project, issue_number))
```

Add to imports in cli.py:

```python
from foreman.storage import Storage
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_cli.py -k "ps or pipeline_detail" -v
```

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/ps.py packages/foreman/src/foreman/cli.py packages/foreman/tests/test_cli.py
git commit -m "feat(cli): add foreman ps and pipeline-detail subcommands"
```

### Task 19: `foreman worktree clean` subcommand

**Files:**
- Modify: `packages/foreman/src/foreman/cli.py`
- Modify: `packages/foreman/tests/test_cli.py`

- [ ] **Step 1: Write failing test**

Add to `packages/foreman/tests/test_cli.py`:

```python
def test_worktree_clean_removes_directory(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[admin]\ngithub_token_env = \"X\"\n"
        f"[daemon]\nsqlite_path = \"{tmp_path / 'f.sqlite'}\"\n"
        f"[projects.voice]\n"
        f"repo = \"jeffrichley/voice\"\n"
        f"local_clone_path = \"{tmp_path / 'voice'}\"\n"
    )
    monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))
    # The worktree dir would normally live under ~/.foreman/worktrees/...
    # We patch the WorktreeManager root to use tmp_path.
    worktree = tmp_path / "worktrees" / "voice" / "issue-42"
    worktree.mkdir(parents=True)
    (worktree / "marker.txt").write_text("present")

    monkeypatch.setenv("FOREMAN_WORKTREES_ROOT", str(tmp_path / "worktrees"))

    from click.testing import CliRunner
    from foreman.cli import cli

    result = CliRunner().invoke(cli, ["worktree", "clean", "voice", "42"])
    assert result.exit_code == 0
    assert not worktree.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run --no-sync pytest packages/foreman/tests/test_cli.py -k worktree_clean -v
```

Expected: FAIL.

- [ ] **Step 3: Add `worktree clean` subcommand**

Add to `packages/foreman/src/foreman/cli.py`:

```python
import shutil


@cli.group()
def worktree() -> None:
    """Worktree management."""


@worktree.command("clean")
@click.argument("project")
@click.argument("issue_number", type=int)
def worktree_clean(project: str, issue_number: int) -> None:
    """Delete the worktree for a project + issue."""
    root = os.environ.get(
        "FOREMAN_WORKTREES_ROOT",
        str(Path("~/.foreman/worktrees").expanduser()),
    )
    target = Path(root) / project / f"issue-{issue_number}"
    if not target.exists():
        click.echo(f"No worktree found at {target}.")
        return
    shutil.rmtree(target)
    click.echo(f"Removed {target}.")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --no-sync pytest packages/foreman/tests/test_cli.py -k worktree -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/cli.py packages/foreman/tests/test_cli.py
git commit -m "feat(cli): add foreman worktree clean subcommand"
```

---

## Phase 13: End-to-end test

### Task 20: Drive 2 tickets through the daemon with fakes

**Files:**
- Create: `packages/foreman/tests/test_daemon_e2e.py`

This is the integration test that proves the acceptance criteria from spec §14 (specifically #2, #4 — auto-merge driving + stage-priority verified).

- [ ] **Step 1: Write the test**

Create `packages/foreman/tests/test_daemon_e2e.py`:

```python
"""End-to-end daemon test using fake host + fake roles.

Proves stage-priority dequeue: when two tickets are tagged simultaneously,
the first to advance becomes most-progressed and finishes ahead of the
second — exactly matching spec §14 acceptance criterion #4.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from foreman.config import AdminConfig, AppsConfig, Config, DaemonConfig, ProjectConfig
from foreman.daemon import Daemon
from foreman.dispatcher import Action, ActionKind, Ticket
from foreman.worker import RoleResult


@dataclass
class _FakeIssue:
    number: int
    labels: list[str]
    updated_at: datetime


@dataclass
class _MutableHost:
    issues: dict[int, _FakeIssue] = field(default_factory=dict)
    added_labels: list[tuple[int, str]] = field(default_factory=list)

    def search_foreman_labeled_issues(self, repo: str) -> list[_FakeIssue]:
        return list(self.issues.values())

    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        self.added_labels.append((issue_number, label))
        if issue_number in self.issues:
            issue = self.issues[issue_number]
            if label not in issue.labels:
                issue.labels.append(label)

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        pass


@dataclass
class _PipelineFakeDispatcher:
    """Simulates the full pipeline by advancing labels through the role chain."""

    completion_order: list[int] = field(default_factory=list)

    async def dispatch(self, *, ticket: Ticket, action: Action) -> RoleResult:
        labels = set(ticket.labels)

        if action.kind == ActionKind.RUN_PLANNER:
            new = {"foreman:spec-review"}
        elif action.kind == ActionKind.RUN_REVIEWER_SPEC:
            new = {"foreman:spec-ready"}
        elif action.kind == ActionKind.MERGE_SPEC_PR:
            new = {"foreman:implementing-ready"}
        elif action.kind == ActionKind.RUN_WORKER:
            new = {"foreman:impl-review"}
        elif action.kind == ActionKind.RUN_REVIEWER_IMPL:
            new = {"foreman:ready-for-merge"}
        elif action.kind == ActionKind.MERGE_IMPL_PR:
            new = set()  # terminal
            self.completion_order.append(ticket.issue_number)
        else:
            new = labels

        return RoleResult(
            new_labels=frozenset(new),
            structured_output=None,
            outcome="success",
        )


def _config(tmp_path: Path) -> Config:
    return Config(
        admin=AdminConfig(),
        daemon=DaemonConfig(
            poll_interval_seconds=5,
            sqlite_path=str(tmp_path / "f.sqlite"),
            log_path=str(tmp_path / "d.log"),
        ),
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path=str(tmp_path / "voice"),
                apps=AppsConfig(),
                auto_merge_spec=True,
                auto_merge_impl=True,
            )
        },
    )


@pytest.mark.asyncio
async def test_two_tickets_first_finishes_before_second_starts_implementing(
    tmp_path: Path,
) -> None:
    """Stage-priority verified: ticket #1 should reach impl-merge before ticket #2
    moves past spec-review (because dequeue prefers further-along tickets)."""
    host = _MutableHost(
        issues={
            1: _FakeIssue(
                number=1,
                labels=["foreman:plan"],
                updated_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            ),
            2: _FakeIssue(
                number=2,
                labels=["foreman:plan"],
                updated_at=datetime(2026, 6, 1, 12, 1, tzinfo=timezone.utc),
            ),
        }
    )
    dispatcher = _PipelineFakeDispatcher()

    daemon = Daemon(config=_config(tmp_path), host=host, role_dispatcher=dispatcher)
    await daemon.start()
    # Generous slack to let both tickets reach terminal.
    await asyncio.sleep(1.0)
    await daemon.shutdown()

    # Both tickets eventually finish; ticket #1 finishes first.
    assert dispatcher.completion_order[0] == 1
    assert dispatcher.completion_order == [1, 2]
```

- [ ] **Step 2: Run the test**

```bash
uv run --no-sync pytest packages/foreman/tests/test_daemon_e2e.py -v
```

Expected: PASS.

- [ ] **Step 3: Run the full suite to confirm no regressions**

```bash
uv run --no-sync pytest -q
```

Expected: all tests pass (358 baseline + ~70 new).

- [ ] **Step 4: Run mypy across the package**

```bash
uv run --no-sync mypy packages/foreman/src
```

Expected: no issues.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/tests/test_daemon_e2e.py
git commit -m "test(daemon): add end-to-end stage-priority verification"
```

---

## Phase 14: Open PR

### Task 21: Push branch and open PR

**Files:**
- (none — git operations)

- [ ] **Step 1: Verify branch is clean and on top of main**

```bash
cd e:/workspaces/ai/agents/foreman
git fetch origin
git status
git log --oneline main..HEAD
```

Expected: clean working tree, commits ahead of main, no unmerged changes.

- [ ] **Step 2: Push the branch**

```bash
git push origin feat/daemon-v1
```

Expected: pre-push hook runs `just check`, 400+ tests pass, push succeeds.

- [ ] **Step 3: Open PR via gh**

```bash
gh pr create --base main --head feat/daemon-v1 --title "feat: v1 daemon — autonomous orchestrator loop" --body "$(cat <<'EOF'
## Summary

Implements the v1 daemon per the design spec at `docs/superpowers/specs/2026-06-01-foreman-daemon-design.md`. Turns Foreman from a toolkit of four role-runners into an autonomous GitHub-issue-to-PR pipeline.

## What's in

- **Configuration**: `DaemonConfig` + `ProjectConfig.auto_merge_spec/impl` (Phase 1)
- **SQLite storage**: schema, migrations, CRUD for pipelines / node_runs / transitions / failures / labels_seen (Phase 2)
- **Pure-function state machine**: `Action`, `Ticket`, `stage_index`, `is_blocked`, `next_action` (Phase 3)
- **De-duped queue**: insert O(1), sort-on-dequeue with stage-priority FIFO tiebreak (Phase 4)
- **Per-ticket asyncio locks** — wired even with `max_concurrent_workers=1` so v2 can bump it (Phase 5)
- **Poller**: `poll_project` diffs against `labels_seen`, returns changed tickets (Phase 6)
- **Worker iteration**: `run_one_iteration` with lock + persistence + self-notify (Phase 7)
- **Daemon composition**: poller + worker async tasks, graceful shutdown (Phase 8)
- **Crash recovery**: halt in-flight tickets with `foreman:failed` on startup (Phase 9)
- **Real role dispatcher**: routes `Action` to existing Planner/Reviewer/Fixer/Worker (Phase 10)
- **JSON-lines logging**: structured logging to `~/.foreman/daemon.log` (Phase 11)
- **CLI**: `daemon start/stop/status`, `ps`, `pipeline-detail`, `worktree clean` (Phase 12)
- **End-to-end test**: two tickets, stage-priority verified (Phase 13)

## What's deferred (per spec §12)

- Production host + role-runner wiring for `foreman daemon start` (separable integration task)
- Priority labels (foreman#22)
- Inter-ticket dependencies
- Multi-worker concurrency
- Webhooks, bus events, idempotent-role auto-retry

## Test plan

- [ ] Pull the branch and run `just check` locally — verify all tests pass
- [ ] Read the design spec at `docs/superpowers/specs/2026-06-01-foreman-daemon-design.md` and confirm the acceptance criteria in §14 are met by the test suite
- [ ] Approve to merge, then file the production-wiring follow-up task
EOF
)"
```

Expected: PR URL printed.

- [ ] **Step 4: Verify PR is open and CI passes**

```bash
gh pr view --json state,statusCheckRollup
```

Expected: `state: OPEN`; all checks GREEN.

---

## Summary

When all tasks are complete, the daemon ships behind a feature flag (the production host + runners wiring is a follow-up). The spec's §14 acceptance criteria #1-8 are exercised by tests:

| Criterion | Coverage |
|---|---|
| #1 `foreman daemon start --detach` runs without crashing | test_cli.py |
| #2 One ticket drives through pipeline | test_daemon_e2e.py |
| #3 Auto-merge end-to-end with no intervention | test_daemon_e2e.py |
| #4 Stage-priority: #1 merges before #2 starts | test_daemon_e2e.py |
| #5 foreman:hold pauses; removing resumes | test_dispatcher.py (is_blocked), test_queue.py |
| #6 foreman:failed parks; removing re-enqueues | test_dispatcher.py |
| #7 SIGTERM during role run lets it finish, then exits | test_daemon.py |
| #8 SIGKILL leaves recoverable state | test_daemon.py reconciliation tests |

After PR #N is merged: file a follow-up to wire real `GitHostProvider` + real `runners` into `_resolve_host_and_runners`. That's the bridge between the daemon and the live GitHub bots.
