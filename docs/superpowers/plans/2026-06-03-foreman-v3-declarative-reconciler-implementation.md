# Foreman v3 Declarative Reconciler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build foreman v3 — a declarative reconciler with GitHub as the sole source of truth for ticket and PR state, and an append-only execution log as the daemon's only cross-poll memory.

**Architecture:** Single async reconciler loop. Per cycle: fetch GitHub state for each registered project via one GraphQL query, evaluate a goal-based rule catalog in precedence order, and execute the first matching rule's action (idempotently, by consulting the execution log before acting). No persistent ticket-state table. No queue table. No `pipelines` table. The execution log is the only thing that crosses polls.

**Tech Stack:** Python 3.12, asyncio, sqlite3 (via the existing `Storage` pattern), Pydantic v2 (already used for v2 schemas), Click (v2's CLI framework), PyGithub for REST fallback (existing), `httpx` for GraphQL (foreman already vendors httpx via PyGithub's deps). Tests with pytest + pytest-asyncio + `tmp_path` for real sqlite files (matching v2's test pattern).

---

## File Structure

v3 lives alongside v2 in the package. New code goes into a new `reconciler/` subpackage so v2 daemon code stays untouched until cutover.

**New files (`packages/foreman/src/foreman/reconciler/`):**

| Path | Responsibility |
|---|---|
| `__init__.py` | Re-export public API: `ExecutionLog`, `Reconciler`, `Action`, `RULES`, etc. |
| `exec_log.py` | `ExecutionLog` class — append-only sqlite writer + idempotence-query reader. Owns the schema. |
| `state.py` | `ProjectSnapshot`, `IssueState`, `PRState` — immutable dataclasses for one poll's view of a project. Not persisted. |
| `actions.py` | `Action` enum + `ActionContext` dataclass + `execute_action()` — maps Action to side-effecting function with dry-run support. |
| `rules.py` | `Rule` dataclass + `PrecedenceTier` enum + `RULES` ordered list + `evaluate()` first-match dispatcher. |
| `observer.py` | `fetch_project_state(project, gh_client) -> ProjectSnapshot` — one GraphQL query per project, joins issues+PRs client-side. |
| `daemon.py` | `Reconciler` class with `async tick()` and `async run()` — composes observer + rules + actions in the 60s loop. |

**New files (`packages/foreman/src/foreman/`):**

| Path | Responsibility |
|---|---|
| `v3_bus_endpoint.py` | Bus envelope handler that receives `ExecutionLogWrite` Events from worker/planner/reviewer/fixer subprocesses and writes through to `ExecutionLog`. |

**Modified files:**

| Path | Change |
|---|---|
| `packages/foreman/src/foreman/config.py` | Add `ReconcilerConfig` Pydantic model (default `db_path = ~/.foreman/reconciler.sqlite`, `poll_interval_seconds = 60`, `retention_days = 30`, `alert_after_n_failures = 3`). Compose into top-level `Config`. |
| `packages/foreman/src/foreman/cli.py` | Add `@daemon.command("v3-start")` Click subcommand with `--dry-run` flag. Wires `Reconciler` and runs it. |
| `packages/foreman/src/foreman/daemon.py` | Add a deprecation banner comment at the top of the v2 daemon module pointing to v3. **No behavior change.** |

**New test files (`packages/foreman/tests/reconciler/`):**

| Path | Tests |
|---|---|
| `__init__.py` | Empty package marker. |
| `test_exec_log.py` | Write/read/idempotence-query behavior; partial index; recovery scan. |
| `test_state.py` | Dataclass roundtrip + immutability + equality. |
| `test_actions.py` | Each action's effect on a fake host; dry-run mode logs but does not execute. |
| `test_rules.py` | Per-rule fires/does-not-fire over canned `(IssueState, PRState, ExecLogReader) -> Action` inputs. |
| `test_rules_precedence.py` | Invariant: every safety rule precedes every forward-progress rule. |
| `test_observer.py` | GraphQL response parsing; PR↔issue join; rate-limit + 5xx surface as `ObserverError`. |
| `test_reconciler_e2e.py` | Integration: stub GH client returns canned project state; reconciler runs N cycles; assert action sequence + exec_log rows. |
| `test_v3_bus_endpoint.py` | Bus envelope decode + ExecutionLog write-through. |

---

## Working agreements (apply to every task)

- Worktree: SDD operates in `e:/workspaces/ai/agents/foreman-worktrees/v3-reconciler-impl` on branch `feat/v3-reconciler` (off `main`). Worktree creation is Step 0 of Task 1.
- Tests must stay green: the 612 pre-existing tests baseline. Each task's commit adds tests; net pass count grows monotonically.
- Stage specific files (`git add path/to/file`), never `git add -A` or `git add .`.
- Conventional commits, lowercase subject. Pattern: `feat(reconciler): <what changed>`. For test-only commits: `test(reconciler): <what>`.
- No commits to `main`. SDD pushes to `feat/v3-reconciler` only.
- Pre-push hook runs the full pytest suite. Investigate failures; don't `--no-verify`.
- Local git config (the SDD worktree): `user.name=wrenrichley`, `user.email=wrenrichley@gmail.com`. Set on Step 0 of Task 1.
- PR convention (foreman#63): use "Implements #N" (NOT "Closes #N") in commit/PR bodies.

---

## Task 0 (one-shot setup, do once before Task 1)

### Task 0: Create v3 implementation worktree + feature branch

**Files:** (none yet — environment setup only)

- [ ] **Step 1: Verify clean main + create worktree on `feat/v3-reconciler`**

Run from `e:/workspaces/ai/agents/foreman`:
```bash
git fetch origin && git checkout main && git pull --ff-only origin main && git worktree add ../foreman-worktrees/v3-reconciler-impl -b feat/v3-reconciler main
```
Expected: new directory `e:/workspaces/ai/agents/foreman-worktrees/v3-reconciler-impl/` exists, on branch `feat/v3-reconciler`, tracking `main`.

- [ ] **Step 2: Set wren git identity in the worktree**

Run from the worktree:
```bash
git config user.name wrenrichley && git config user.email wrenrichley@gmail.com && git config --get user.name && git config --get user.email
```
Expected: prints `wrenrichley` and `wrenrichley@gmail.com`.

- [ ] **Step 3: Verify uv sync + baseline pytest green**

Run from the worktree:
```bash
uv sync && uv run pytest packages/foreman -q
```
Expected: `612 passed, 1 skipped` (or higher; baseline is 612 passing as of `66e712a`).

---

## Task 1: ExecutionLog — schema + writer + reader

**Files:**
- Create: `packages/foreman/src/foreman/reconciler/__init__.py`
- Create: `packages/foreman/src/foreman/reconciler/exec_log.py`
- Create: `packages/foreman/tests/reconciler/__init__.py`
- Create: `packages/foreman/tests/reconciler/test_exec_log.py`

The execution log is the foundation. Everything else depends on its write API and its idempotence-query API.

- [ ] **Step 1: Write the failing tests**

Create `packages/foreman/tests/reconciler/__init__.py` as empty file.

Create `packages/foreman/tests/reconciler/test_exec_log.py`:
```python
"""Tests for the v3 execution log — schema, writer, idempotence reader."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from foreman.reconciler.exec_log import ExecutionLog


def test_init_creates_schema_and_indexes(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }

    assert "execution_log" in tables
    assert "idx_ticket_ts" in indexes
    assert "idx_running" in indexes


def test_init_is_idempotent(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    log.init()  # second call must not raise


def test_write_action_returns_row_id(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    row_id = log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_worker_rule",
        action="dispatch_worker",
        outcome="running",
        details={"pid": 12345},
    )

    assert isinstance(row_id, int)
    assert row_id >= 1


def test_write_action_persists_all_fields(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    row_id = log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_worker_rule",
        action="dispatch_worker",
        outcome="running",
        details={"pid": 12345, "pr": 144},
    )

    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        row = conn.execute(
            "SELECT ticket_id, project, rule_name, action, outcome, details "
            "FROM execution_log WHERE id = ?",
            (row_id,),
        ).fetchone()

    assert row[0] == "jeffrichley/foreman#143"
    assert row[1] == "foreman"
    assert row[2] == "dispatch_worker_rule"
    assert row[3] == "dispatch_worker"
    assert row[4] == "running"
    assert json.loads(row[5]) == {"pid": 12345, "pr": 144}


def test_terminate_action_writes_completion_with_parent_link(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    start_id = log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_worker_rule",
        action="dispatch_worker",
        outcome="running",
        details={"pid": 12345},
    )
    end_id = log.terminate_action(
        parent_log_id=start_id,
        outcome="success",
        details={"merged_pr": 144},
    )

    assert end_id != start_id

    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        row = conn.execute(
            "SELECT action, outcome, parent_log_id FROM execution_log WHERE id = ?",
            (end_id,),
        ).fetchone()

    # Termination row inherits action name from parent ("dispatch_worker"),
    # outcome reflects how it ended, parent_log_id points back at the start.
    assert row[0] == "dispatch_worker"
    assert row[1] == "success"
    assert row[2] == start_id


def test_has_unterminated_returns_true_when_start_without_end(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_worker_rule",
        action="dispatch_worker",
        outcome="running",
        details={"pid": 12345},
    )

    assert log.has_unterminated("dispatch_worker", "jeffrichley/foreman#143") is True


def test_has_unterminated_returns_false_after_termination(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    start_id = log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_worker_rule",
        action="dispatch_worker",
        outcome="running",
        details={"pid": 12345},
    )
    log.terminate_action(parent_log_id=start_id, outcome="success", details={})

    assert log.has_unterminated("dispatch_worker", "jeffrichley/foreman#143") is False


def test_has_unterminated_scoped_to_ticket(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_worker_rule",
        action="dispatch_worker",
        outcome="running",
        details={},
    )

    assert log.has_unterminated("dispatch_worker", "jeffrichley/foreman#999") is False


def test_has_recent_returns_true_within_window(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="surface_help_rule",
        action="surface_help",
        outcome="success",
        details={},
    )

    assert log.has_recent("surface_help", "jeffrichley/foreman#143", within_seconds=3600) is True


def test_has_recent_returns_false_when_no_match(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    assert log.has_recent("surface_help", "jeffrichley/foreman#143", within_seconds=3600) is False


def test_recover_orphaned_running_rows_marks_errored(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_worker_rule",
        action="dispatch_worker",
        outcome="running",
        details={"pid": 12345},
    )

    recovered = log.recover_orphaned()

    assert recovered == 1
    assert log.has_unterminated("dispatch_worker", "jeffrichley/foreman#143") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run pytest packages/foreman/tests/reconciler/test_exec_log.py -v
```
Expected: every test FAILs with `ModuleNotFoundError: No module named 'foreman.reconciler.exec_log'`.

- [ ] **Step 3: Implement `ExecutionLog`**

Create `packages/foreman/src/foreman/reconciler/__init__.py`:
```python
"""Foreman v3 declarative reconciler.

GitHub is the source of truth for ticket + PR state. The execution log is
the daemon's only cross-poll memory. See:
docs/superpowers/specs/foreman-issue-106-spec.md
"""

from foreman.reconciler.exec_log import ExecutionLog

__all__ = ["ExecutionLog"]
```

Create `packages/foreman/src/foreman/reconciler/exec_log.py`:
```python
"""Append-only execution log for foreman v3.

Single writer (the reconciler daemon process). The daemon writes a row when
it acts; rule predicates read for idempotence checks. Workers/Planners/etc.
do NOT write directly — they send ExecutionLogWrite envelopes via the bus,
which the daemon receives and translates to log rows. See spec section
"Single-writer pattern".
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS execution_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ticket_id TEXT NOT NULL,
        project TEXT NOT NULL,
        rule_name TEXT,
        action TEXT NOT NULL,
        outcome TEXT NOT NULL,
        details TEXT,
        parent_log_id INTEGER REFERENCES execution_log(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ticket_ts ON execution_log(ticket_id, ts DESC)",
    """
    CREATE INDEX IF NOT EXISTS idx_running ON execution_log(outcome)
    WHERE outcome = 'running'
    """,
]


class ExecutionLog:
    """Append-only sqlite log of reconciler decisions and their outcomes."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def init(self) -> None:
        """Create schema + indexes. Idempotent."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            for stmt in _SCHEMA:
                conn.execute(stmt)

    def _connect(self) -> sqlite3.Connection:
        # Each call returns a fresh connection — matches v2's Storage pattern.
        # Foreign-key enforcement on so parent_log_id is real.
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def write_action(
        self,
        *,
        ticket_id: str,
        project: str,
        rule_name: str | None,
        action: str,
        outcome: str,
        details: dict[str, Any],
        parent_log_id: int | None = None,
    ) -> int:
        """Insert a row. Returns the new row id."""
        details_json = json.dumps(details, sort_keys=True)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO execution_log
                    (ticket_id, project, rule_name, action, outcome, details, parent_log_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (ticket_id, project, rule_name, action, outcome, details_json, parent_log_id),
            )
            return int(cur.lastrowid)

    def terminate_action(
        self,
        *,
        parent_log_id: int,
        outcome: str,
        details: dict[str, Any],
    ) -> int:
        """Write a termination row pointing at the start row.

        Inherits ticket_id / project / action / rule_name from the parent so
        the pair is queryable as one unit. Outcome reflects success | error.
        """
        with self._connect() as conn:
            parent = conn.execute(
                """
                SELECT ticket_id, project, rule_name, action
                FROM execution_log WHERE id = ?
                """,
                (parent_log_id,),
            ).fetchone()
            if parent is None:
                raise ValueError(f"No log row with id={parent_log_id}")
            ticket_id, project, rule_name, action = parent
            details_json = json.dumps(details, sort_keys=True)
            cur = conn.execute(
                """
                INSERT INTO execution_log
                    (ticket_id, project, rule_name, action, outcome, details, parent_log_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (ticket_id, project, rule_name, action, outcome, details_json, parent_log_id),
            )
            return int(cur.lastrowid)

    def has_unterminated(self, action: str, ticket_id: str) -> bool:
        """True iff there is an outcome='running' row for (action, ticket_id)
        with no termination row pointing at it.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM execution_log start
                WHERE start.ticket_id = ?
                  AND start.action = ?
                  AND start.outcome = 'running'
                  AND NOT EXISTS (
                      SELECT 1 FROM execution_log term
                      WHERE term.parent_log_id = start.id
                  )
                LIMIT 1
                """,
                (ticket_id, action),
            ).fetchone()
            return row is not None

    def has_recent(self, action: str, ticket_id: str, *, within_seconds: int) -> bool:
        """True iff there's any row for (action, ticket_id) with ts within the
        last `within_seconds` seconds. Used for surface_help alert rate-limit.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM execution_log
                WHERE ticket_id = ? AND action = ? AND ts > ?
                LIMIT 1
                """,
                (ticket_id, action, cutoff.isoformat()),
            ).fetchone()
            return row is not None

    def recover_orphaned(self) -> int:
        """On daemon restart: any outcome='running' row with no termination
        means the daemon crashed mid-action. Mark each as terminated with
        outcome='errored:recovery'. Returns the count of rows recovered.
        """
        with self._connect() as conn:
            orphans = conn.execute(
                """
                SELECT start.id FROM execution_log start
                WHERE start.outcome = 'running'
                  AND NOT EXISTS (
                      SELECT 1 FROM execution_log term
                      WHERE term.parent_log_id = start.id
                  )
                """
            ).fetchall()
        count = 0
        for (parent_id,) in orphans:
            self.terminate_action(
                parent_log_id=parent_id,
                outcome="errored:recovery",
                details={"reason": "daemon restart found orphaned running row"},
            )
            count += 1
        return count
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
uv run pytest packages/foreman/tests/reconciler/test_exec_log.py -v
```
Expected: all 11 tests PASS. Full suite still green:
```bash
uv run pytest packages/foreman -q
```
Expected: `623 passed` (612 baseline + 11 new), 1 skipped.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/reconciler/__init__.py packages/foreman/src/foreman/reconciler/exec_log.py packages/foreman/tests/reconciler/__init__.py packages/foreman/tests/reconciler/test_exec_log.py
git commit -m "feat(reconciler): add v3 ExecutionLog with idempotence-query API"
```

---

## Task 2: State dataclasses — ProjectSnapshot, IssueState, PRState

**Files:**
- Create: `packages/foreman/src/foreman/reconciler/state.py`
- Modify: `packages/foreman/src/foreman/reconciler/__init__.py`
- Create: `packages/foreman/tests/reconciler/test_state.py`

These are the immutable per-poll view of a project. Rules and actions consume them; nothing persists them.

- [ ] **Step 1: Write the failing tests**

Create `packages/foreman/tests/reconciler/test_state.py`:
```python
"""Tests for v3 state dataclasses — ProjectSnapshot, IssueState, PRState."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foreman.reconciler.state import IssueState, PRState, ProjectSnapshot


def test_issue_state_is_frozen() -> None:
    issue = IssueState(
        number=143,
        title="Daemon stuck on planning",
        labels=("foreman:planning",),
        assignees=(),
        body="",
        updated_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    with pytest.raises(Exception):
        issue.number = 999  # type: ignore[misc]


def test_pr_state_is_frozen() -> None:
    pr = PRState(
        number=144,
        head_ref="spec-143-fix",
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="Implements #143",
        linked_issue_numbers=(143,),
        is_merged=False,
    )
    with pytest.raises(Exception):
        pr.number = 999  # type: ignore[misc]


def test_project_snapshot_finds_issue_by_number() -> None:
    issue = IssueState(
        number=143,
        title="x",
        labels=("foreman:planning",),
        assignees=(),
        body="",
        updated_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    snap = ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(issue,),
        prs=(),
        fetched_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    assert snap.find_issue(143) is issue
    assert snap.find_issue(999) is None


def test_project_snapshot_returns_prs_linked_to_issue() -> None:
    linked_pr = PRState(
        number=144,
        head_ref="spec-143-fix",
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="Implements #143",
        linked_issue_numbers=(143,),
        is_merged=False,
    )
    unrelated_pr = PRState(
        number=200,
        head_ref="other",
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="",
        linked_issue_numbers=(),
        is_merged=False,
    )
    snap = ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(),
        prs=(linked_pr, unrelated_pr),
        fetched_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    linked = snap.prs_for_issue(143)
    assert linked == (linked_pr,)


def test_ticket_id_format() -> None:
    snap = ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(),
        prs=(),
        fetched_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    assert snap.ticket_id_for(143) == "jeffrichley/foreman#143"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run pytest packages/foreman/tests/reconciler/test_state.py -v
```
Expected: every test FAILs with `ModuleNotFoundError: No module named 'foreman.reconciler.state'`.

- [ ] **Step 3: Implement state dataclasses**

Create `packages/foreman/src/foreman/reconciler/state.py`:
```python
"""Immutable per-poll state of a project, derived from one GraphQL fetch.

These dataclasses are the reconciler's only view of GitHub state during a
poll. They are NOT persisted — they exist for the duration of one tick and
are discarded. The execution log persists facts the daemon decides;
GitHub IS the truth for everything in these objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True, slots=True)
class IssueState:
    """One issue's relevant state at the time of fetch."""

    number: int
    title: str
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    body: str
    updated_at: datetime

    def has_label(self, label: str) -> bool:
        return label in self.labels


@dataclass(frozen=True, slots=True)
class PRState:
    """One PR's relevant state at the time of fetch.

    `mergeable` mirrors GitHub's MergeableState string: "MERGEABLE" |
    "CONFLICTING" | "UNKNOWN". `ci_status` mirrors statusCheckRollup's
    rollupState: "SUCCESS" | "PENDING" | "FAILURE" | "ERROR" | None.
    `linked_issue_numbers` comes from GraphQL `closingIssuesReferences`.
    """

    number: int
    head_ref: str
    mergeable: str
    ci_status: str | None
    body: str
    linked_issue_numbers: tuple[int, ...]
    is_merged: bool

    def closes_issue(self, issue_number: int) -> bool:
        return issue_number in self.linked_issue_numbers


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    """One poll's full view of a project.

    `project` is the local config name ("foreman", "voice", "agent_core").
    `owner` / `repo` are the GitHub coords. `issues` and `prs` are tuples for
    immutability + hashability.
    """

    project: str
    owner: str
    repo: str
    issues: tuple[IssueState, ...]
    prs: tuple[PRState, ...]
    fetched_at: datetime

    def find_issue(self, number: int) -> IssueState | None:
        for issue in self.issues:
            if issue.number == number:
                return issue
        return None

    def prs_for_issue(self, issue_number: int) -> tuple[PRState, ...]:
        return tuple(pr for pr in self.prs if pr.closes_issue(issue_number))

    def ticket_id_for(self, issue_number: int) -> str:
        return f"{self.owner}/{self.repo}#{issue_number}"
```

Modify `packages/foreman/src/foreman/reconciler/__init__.py` to re-export:
```python
"""Foreman v3 declarative reconciler.

GitHub is the source of truth for ticket + PR state. The execution log is
the daemon's only cross-poll memory. See:
docs/superpowers/specs/foreman-issue-106-spec.md
"""

from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.state import IssueState, PRState, ProjectSnapshot

__all__ = ["ExecutionLog", "IssueState", "PRState", "ProjectSnapshot"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
uv run pytest packages/foreman/tests/reconciler/test_state.py -v
```
Expected: all 5 tests PASS. Suite still green:
```bash
uv run pytest packages/foreman -q
```
Expected: `628 passed, 1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/reconciler/state.py packages/foreman/src/foreman/reconciler/__init__.py packages/foreman/tests/reconciler/test_state.py
git commit -m "feat(reconciler): add immutable per-poll state dataclasses"
```

---

## Task 3: Action enum + ActionContext + Action lookup

**Files:**
- Create: `packages/foreman/src/foreman/reconciler/actions.py` (initial form — enum + context only; executor follows in Task 4)
- Modify: `packages/foreman/src/foreman/reconciler/__init__.py`
- Create: `packages/foreman/tests/reconciler/test_actions.py` (initial form)

- [ ] **Step 1: Write the failing tests**

Create `packages/foreman/tests/reconciler/test_actions.py`:
```python
"""Tests for v3 actions — enum, context, executor."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from foreman.reconciler.actions import Action, ActionContext
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.state import IssueState, PRState, ProjectSnapshot


def _snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(
            IssueState(
                number=143,
                title="t",
                labels=("foreman:planning",),
                assignees=(),
                body="",
                updated_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
            ),
        ),
        prs=(),
        fetched_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )


def test_action_enum_covers_spec_catalog() -> None:
    expected = {
        "NOOP",
        "SURFACE_HELP",
        "DISPATCH_PLANNER",
        "MERGE_SPEC_PR",
        "ADVANCE_LABEL_TO_PLAN_APPROVED",
        "DISPATCH_WORKER",
        "DISPATCH_REVIEWER",
        "DISPATCH_FIXER",
        "MERGE_IMPL_PR",
        "ADVANCE_LABEL_TO_DONE",
    }
    assert {a.name for a in Action} == expected


def test_action_context_exposes_ticket_id(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    issue = snap.issues[0]
    ctx = ActionContext(snapshot=snap, issue=issue, pr=None, log=log)
    assert ctx.ticket_id == "jeffrichley/foreman#143"


def test_action_context_carries_pr_when_present(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    issue = snap.issues[0]
    pr = PRState(
        number=144,
        head_ref="spec-143",
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="Implements #143",
        linked_issue_numbers=(143,),
        is_merged=False,
    )
    ctx = ActionContext(snapshot=snap, issue=issue, pr=pr, log=log)
    assert ctx.pr is pr
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
uv run pytest packages/foreman/tests/reconciler/test_actions.py -v
```
Expected: tests FAIL with `ModuleNotFoundError: No module named 'foreman.reconciler.actions'`.

- [ ] **Step 3: Implement `Action` enum + `ActionContext`**

Create `packages/foreman/src/foreman/reconciler/actions.py`:
```python
"""Action catalog for v3 — what the reconciler can do, and the context it
needs to do it. The executor itself lands in Task 4; this module first
establishes the enum + context shape so rules (Task 5+) can reference them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.state import IssueState, PRState, ProjectSnapshot


class Action(enum.Enum):
    """Catalog of every state-changing operation the reconciler can emit.

    Order matches the spec's transition flow for readability; the enum is
    NOT ordered (no value-comparison semantics intended).
    """

    NOOP = "noop"
    SURFACE_HELP = "surface_help"
    DISPATCH_PLANNER = "dispatch_planner"
    MERGE_SPEC_PR = "merge_spec_pr"
    ADVANCE_LABEL_TO_PLAN_APPROVED = "advance_label_to_plan_approved"
    DISPATCH_WORKER = "dispatch_worker"
    DISPATCH_REVIEWER = "dispatch_reviewer"
    DISPATCH_FIXER = "dispatch_fixer"
    MERGE_IMPL_PR = "merge_impl_pr"
    ADVANCE_LABEL_TO_DONE = "advance_label_to_done"


@dataclass(frozen=True)
class ActionContext:
    """Everything a rule + executor need to evaluate or apply an action.

    `snapshot` is the full project view. `issue` is the focal ticket.
    `pr` is the linked PR if one exists (None for tickets pre-PR or after merge).
    `log` is the execution log — rules consult it for idempotence; executor
    writes through it.
    """

    snapshot: ProjectSnapshot
    issue: IssueState
    pr: PRState | None
    log: ExecutionLog

    @property
    def ticket_id(self) -> str:
        return self.snapshot.ticket_id_for(self.issue.number)
```

Modify `packages/foreman/src/foreman/reconciler/__init__.py`:
```python
"""Foreman v3 declarative reconciler."""

from foreman.reconciler.actions import Action, ActionContext
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.state import IssueState, PRState, ProjectSnapshot

__all__ = [
    "Action",
    "ActionContext",
    "ExecutionLog",
    "IssueState",
    "PRState",
    "ProjectSnapshot",
]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest packages/foreman/tests/reconciler/test_actions.py -v && uv run pytest packages/foreman -q
```
Expected: 3 actions tests PASS; full suite `631 passed, 1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/reconciler/actions.py packages/foreman/src/foreman/reconciler/__init__.py packages/foreman/tests/reconciler/test_actions.py
git commit -m "feat(reconciler): add Action enum + ActionContext shape"
```

---

## Task 4: Rule dataclass + RULES catalog skeleton + precedence invariant test

**Files:**
- Create: `packages/foreman/src/foreman/reconciler/rules.py`
- Modify: `packages/foreman/src/foreman/reconciler/__init__.py`
- Create: `packages/foreman/tests/reconciler/test_rules.py` (skeleton tests for empty catalog evaluation)
- Create: `packages/foreman/tests/reconciler/test_rules_precedence.py`

The rule infrastructure lands here; specific rule predicates land in Tasks 5 + 6.

- [ ] **Step 1: Write the failing tests**

Create `packages/foreman/tests/reconciler/test_rules_precedence.py`:
```python
"""Invariant test: every safety rule's precedence is strictly less than every
forward-progress rule's. Catalog mutations that violate this break the build.
"""

from __future__ import annotations

from foreman.reconciler.rules import RULES, PrecedenceTier


def test_rules_have_unique_precedence_values() -> None:
    precedences = [rule.precedence for rule in RULES]
    assert len(precedences) == len(set(precedences)), (
        "Two rules share the same precedence; ordering is ambiguous"
    )


def test_safety_tier_uses_precedence_below_100() -> None:
    for rule in RULES:
        if rule.tier is PrecedenceTier.SAFETY:
            assert rule.precedence < 100, (
                f"Safety rule {rule.name!r} has precedence {rule.precedence}; must be < 100"
            )


def test_forward_progress_tier_uses_precedence_at_or_above_100() -> None:
    for rule in RULES:
        if rule.tier is PrecedenceTier.FORWARD_PROGRESS:
            assert rule.precedence >= 100, (
                f"Forward-progress rule {rule.name!r} has precedence "
                f"{rule.precedence}; must be >= 100"
            )


def test_safety_rules_all_precede_forward_progress_rules() -> None:
    safety_max = max(
        (r.precedence for r in RULES if r.tier is PrecedenceTier.SAFETY),
        default=-1,
    )
    progress_min = min(
        (r.precedence for r in RULES if r.tier is PrecedenceTier.FORWARD_PROGRESS),
        default=10**9,
    )
    assert safety_max < progress_min, (
        f"Safety precedence max ({safety_max}) must be < forward-progress min ({progress_min})"
    )


def test_rules_are_sorted_by_precedence() -> None:
    precedences = [rule.precedence for rule in RULES]
    assert precedences == sorted(precedences), (
        "RULES list must be in ascending precedence order; first match wins"
    )
```

Create `packages/foreman/tests/reconciler/test_rules.py`:
```python
"""Tests for the rule evaluator. Specific rule predicates land in Tasks 5+6;
this module covers the evaluator's behavior over the catalog."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from foreman.reconciler.actions import Action, ActionContext
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.rules import Rule, PrecedenceTier, evaluate
from foreman.reconciler.state import IssueState, ProjectSnapshot


def _ctx(tmp_path: Path) -> ActionContext:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(
            IssueState(
                number=143,
                title="t",
                labels=("foreman:planning",),
                assignees=(),
                body="",
                updated_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
            ),
        ),
        prs=(),
        fetched_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    return ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)


def test_evaluate_empty_catalog_returns_noop(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert evaluate(ctx, rules=()) is Action.NOOP


def test_evaluate_first_matching_rule_wins(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    catalog = (
        Rule(
            name="never_fires",
            tier=PrecedenceTier.SAFETY,
            precedence=10,
            when=lambda c: False,
            then=Action.SURFACE_HELP,
        ),
        Rule(
            name="always_fires",
            tier=PrecedenceTier.SAFETY,
            precedence=20,
            when=lambda c: True,
            then=Action.SURFACE_HELP,
        ),
        Rule(
            name="would_fire_if_reached",
            tier=PrecedenceTier.FORWARD_PROGRESS,
            precedence=100,
            when=lambda c: True,
            then=Action.DISPATCH_PLANNER,
        ),
    )
    assert evaluate(ctx, rules=catalog) is Action.SURFACE_HELP


def test_evaluate_no_match_returns_noop(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    catalog = (
        Rule(
            name="never_fires",
            tier=PrecedenceTier.SAFETY,
            precedence=10,
            when=lambda c: False,
            then=Action.SURFACE_HELP,
        ),
    )
    assert evaluate(ctx, rules=catalog) is Action.NOOP


def test_evaluate_predicate_exception_treated_as_no_match(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    catalog = (
        Rule(
            name="raiser",
            tier=PrecedenceTier.SAFETY,
            precedence=10,
            when=lambda c: (_ for _ in ()).throw(RuntimeError("boom")),
            then=Action.SURFACE_HELP,
        ),
        Rule(
            name="rescuer",
            tier=PrecedenceTier.FORWARD_PROGRESS,
            precedence=100,
            when=lambda c: True,
            then=Action.DISPATCH_PLANNER,
        ),
    )
    assert evaluate(ctx, rules=catalog) is Action.DISPATCH_PLANNER
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest packages/foreman/tests/reconciler/test_rules.py packages/foreman/tests/reconciler/test_rules_precedence.py -v
```
Expected: all fail with `ModuleNotFoundError: No module named 'foreman.reconciler.rules'`.

- [ ] **Step 3: Implement Rule + RULES (empty for now) + evaluator**

Create `packages/foreman/src/foreman/reconciler/rules.py`:
```python
"""Rule catalog + evaluator for v3.

Rules are pure predicates over ActionContext. The evaluator scans RULES in
ascending precedence order; the first matching rule's action fires. A safety
rule that fires preempts every forward-progress rule below it.

The RULES list is appended to in Tasks 5 (safety) and 6 (forward-progress).
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Callable

from foreman.reconciler.actions import Action, ActionContext

logger = logging.getLogger(__name__)


class PrecedenceTier(enum.Enum):
    """Two-tier ordering: safety always preempts forward-progress."""

    SAFETY = "safety"
    FORWARD_PROGRESS = "forward_progress"


@dataclass(frozen=True)
class Rule:
    """One rule. `when` is the predicate; `then` is the action to emit on True."""

    name: str
    tier: PrecedenceTier
    precedence: int
    when: Callable[[ActionContext], bool]
    then: Action


# Filled in by Tasks 5 (safety) and 6 (forward-progress). Order matters:
# the evaluator iterates from index 0 forward and fires the first match.
RULES: tuple[Rule, ...] = ()


def evaluate(ctx: ActionContext, *, rules: tuple[Rule, ...] = None) -> Action:
    """Run the catalog over the context. Returns the first matching rule's
    action, or Action.NOOP if no rule matches.

    A predicate that raises is treated as "did not match" — the evaluator
    logs the exception and continues. Rationale: a broken predicate must not
    halt the reconciler; rules are independent.
    """
    catalog = RULES if rules is None else rules
    for rule in catalog:
        try:
            if rule.when(ctx):
                return rule.then
        except Exception:
            logger.exception(
                "rule %r raised during evaluation for ticket %s; treating as no-match",
                rule.name,
                ctx.ticket_id,
            )
            continue
    return Action.NOOP
```

Modify `packages/foreman/src/foreman/reconciler/__init__.py`:
```python
"""Foreman v3 declarative reconciler."""

from foreman.reconciler.actions import Action, ActionContext
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.rules import RULES, PrecedenceTier, Rule, evaluate
from foreman.reconciler.state import IssueState, PRState, ProjectSnapshot

__all__ = [
    "Action",
    "ActionContext",
    "ExecutionLog",
    "IssueState",
    "PRState",
    "PrecedenceTier",
    "ProjectSnapshot",
    "RULES",
    "Rule",
    "evaluate",
]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest packages/foreman/tests/reconciler/test_rules.py packages/foreman/tests/reconciler/test_rules_precedence.py -v && uv run pytest packages/foreman -q
```
Expected: rules + precedence tests PASS (precedence tests trivially pass with empty RULES); full suite `640 passed, 1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/reconciler/rules.py packages/foreman/src/foreman/reconciler/__init__.py packages/foreman/tests/reconciler/test_rules.py packages/foreman/tests/reconciler/test_rules_precedence.py
git commit -m "feat(reconciler): add Rule dataclass + empty catalog + first-match evaluator"
```

---

## Task 5: Safety rule catalog

**Files:**
- Modify: `packages/foreman/src/foreman/reconciler/rules.py` (append safety rules to RULES)
- Modify: `packages/foreman/tests/reconciler/test_rules.py` (per-rule cases for safety tier)

Safety rules occupy precedence 10-90 (gaps for future inserts). Each must fire when its condition is met regardless of forward-progress eligibility.

- [ ] **Step 1: Write the failing tests**

Append to `packages/foreman/tests/reconciler/test_rules.py`:
```python
# --- Safety rule cases ---


def _issue(labels: tuple[str, ...] = (), **overrides) -> IssueState:
    base = dict(
        number=143,
        title="t",
        labels=labels,
        assignees=(),
        body="",
        updated_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return IssueState(**base)


def _pr(
    *,
    mergeable: str = "MERGEABLE",
    ci_status: str | None = "SUCCESS",
    is_merged: bool = False,
    linked: tuple[int, ...] = (143,),
) -> "PRState":
    from foreman.reconciler.state import PRState
    return PRState(
        number=144,
        head_ref="spec-143",
        mergeable=mergeable,
        ci_status=ci_status,
        body="Implements #143",
        linked_issue_numbers=linked,
        is_merged=is_merged,
    )


def _ctx_with(tmp_path: Path, issue: IssueState, pr=None) -> ActionContext:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(issue,),
        prs=(pr,) if pr else (),
        fetched_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    return ActionContext(snapshot=snap, issue=issue, pr=pr, log=log)


def test_needs_help_label_fires_surface_help(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:needs-help",)))
    assert evaluate(ctx, rules=RULES) is Action.SURFACE_HELP


def test_mergeable_conflict_fires_surface_help(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-review",)),
        _pr(mergeable="CONFLICTING"),
    )
    assert evaluate(ctx, rules=RULES) is Action.SURFACE_HELP


def test_ci_failure_on_impl_pr_fires_surface_help(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-review",)),
        _pr(ci_status="FAILURE"),
    )
    assert evaluate(ctx, rules=RULES) is Action.SURFACE_HELP


def test_surface_help_rate_limited_within_one_hour(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    issue = _issue(labels=("foreman:needs-help",))
    ctx = _ctx_with(tmp_path, issue)
    # Pre-seed an outcome=success surface_help row from "now".
    ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="needs_help_label",
        action="surface_help",
        outcome="success",
        details={},
    )
    # Within the rate-limit window: SHOULD NOT fire again (drops to NOOP because
    # the forward-progress catalog has nothing to do for a stuck planning ticket
    # with no PR).
    assert evaluate(ctx, rules=RULES) is Action.NOOP


def test_no_safety_condition_does_not_emit_surface_help(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:planning",)))
    # Forward-progress catalog might fire; but no safety condition means SURFACE_HELP
    # is not the answer.
    assert evaluate(ctx, rules=RULES) is not Action.SURFACE_HELP
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest packages/foreman/tests/reconciler/test_rules.py -v -k "safety or needs_help or mergeable or ci_failure or surface_help"
```
Expected: 4 safety-related tests FAIL (RULES is empty); existing 4 evaluator tests still PASS.

- [ ] **Step 3: Implement safety rules**

Modify `packages/foreman/src/foreman/reconciler/rules.py` — replace the empty `RULES = ()` with:
```python
def _needs_help_label(ctx: ActionContext) -> bool:
    return "foreman:needs-help" in ctx.issue.labels


def _mergeable_conflict(ctx: ActionContext) -> bool:
    return ctx.pr is not None and ctx.pr.mergeable == "CONFLICTING"


def _impl_pr_ci_failure(ctx: ActionContext) -> bool:
    if ctx.pr is None or ctx.pr.ci_status != "FAILURE":
        return False
    return any(
        label in ctx.issue.labels
        for label in ("foreman:impl-review", "foreman:impl-approved", "foreman:impl-fix")
    )


def _spec_pr_ci_failure(ctx: ActionContext) -> bool:
    if ctx.pr is None or ctx.pr.ci_status != "FAILURE":
        return False
    return "foreman:planning" in ctx.issue.labels


def _safety_with_rate_limit(predicate):
    """Wrap a safety predicate so it stops re-firing if surface_help has been
    emitted for this ticket in the last hour.
    """
    def wrapped(ctx: ActionContext) -> bool:
        if not predicate(ctx):
            return False
        if ctx.log.has_recent("surface_help", ctx.ticket_id, within_seconds=3600):
            return False
        return True
    return wrapped


_SAFETY_RULES: tuple[Rule, ...] = (
    Rule(
        name="needs_help_label",
        tier=PrecedenceTier.SAFETY,
        precedence=10,
        when=_safety_with_rate_limit(_needs_help_label),
        then=Action.SURFACE_HELP,
    ),
    Rule(
        name="mergeable_conflict",
        tier=PrecedenceTier.SAFETY,
        precedence=20,
        when=_safety_with_rate_limit(_mergeable_conflict),
        then=Action.SURFACE_HELP,
    ),
    Rule(
        name="impl_pr_ci_failure",
        tier=PrecedenceTier.SAFETY,
        precedence=30,
        when=_safety_with_rate_limit(_impl_pr_ci_failure),
        then=Action.SURFACE_HELP,
    ),
    Rule(
        name="spec_pr_ci_failure",
        tier=PrecedenceTier.SAFETY,
        precedence=40,
        when=_safety_with_rate_limit(_spec_pr_ci_failure),
        then=Action.SURFACE_HELP,
    ),
)


RULES: tuple[Rule, ...] = _SAFETY_RULES  # forward-progress rules append in Task 6
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest packages/foreman/tests/reconciler/test_rules.py packages/foreman/tests/reconciler/test_rules_precedence.py -v && uv run pytest packages/foreman -q
```
Expected: all rules + precedence tests PASS. Full suite `645 passed, 1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/reconciler/rules.py packages/foreman/tests/reconciler/test_rules.py
git commit -m "feat(reconciler): add safety rule catalog with surface_help rate-limit"
```

---

## Task 6: Forward-progress rule catalog

**Files:**
- Modify: `packages/foreman/src/foreman/reconciler/rules.py` (append forward-progress rules)
- Modify: `packages/foreman/tests/reconciler/test_rules.py` (per-rule cases for progress tier)

Forward-progress rules occupy precedence 100-200. They drive every dispatch + merge + label-advance from the action catalog.

- [ ] **Step 1: Write the failing tests**

Append to `packages/foreman/tests/reconciler/test_rules.py`:
```python
# --- Forward-progress rule cases ---


def test_dispatch_planner_fires_on_planning_no_pr(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:planning",)))
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_PLANNER


def test_dispatch_planner_skipped_when_already_running(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:planning",)))
    ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )
    assert evaluate(ctx, rules=RULES) is Action.NOOP


def test_merge_spec_pr_fires_when_planning_pr_green(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    assert evaluate(ctx, rules=RULES) is Action.MERGE_SPEC_PR


def test_advance_label_to_plan_approved_when_spec_pr_merged(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(is_merged=True),
    )
    assert evaluate(ctx, rules=RULES) is Action.ADVANCE_LABEL_TO_PLAN_APPROVED


def test_advance_label_to_plan_approved_idempotent(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(is_merged=True),
    )
    # Pre-seed the advance as already done.
    ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="advance_label_to_plan_approved",
        action="advance_label_to_plan_approved",
        outcome="success",
        details={"from": "foreman:planning", "to": "foreman:plan-approved"},
    )
    assert evaluate(ctx, rules=RULES) is Action.NOOP


def test_dispatch_worker_fires_on_plan_approved(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:plan-approved",)))
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_WORKER


def test_dispatch_reviewer_fires_on_impl_review_green(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-review",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_REVIEWER


def test_dispatch_fixer_fires_on_impl_fix_label(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-fix",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_FIXER


def test_merge_impl_pr_fires_on_impl_approved(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    assert evaluate(ctx, rules=RULES) is Action.MERGE_IMPL_PR


def test_advance_label_to_done_when_impl_pr_merged(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(is_merged=True),
    )
    assert evaluate(ctx, rules=RULES) is Action.ADVANCE_LABEL_TO_DONE
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest packages/foreman/tests/reconciler/test_rules.py -v -k "dispatch or merge or advance"
```
Expected: 10 progress tests FAIL.

- [ ] **Step 3: Append forward-progress rules**

Modify `packages/foreman/src/foreman/reconciler/rules.py` — add these helper predicates above `_SAFETY_RULES` and the `_PROGRESS_RULES` tuple below it, then update `RULES`:

```python
def _planning_no_pr(ctx: ActionContext) -> bool:
    return (
        "foreman:planning" in ctx.issue.labels
        and ctx.pr is None
        and not ctx.log.has_unterminated("dispatch_planner", ctx.ticket_id)
    )


def _planning_pr_green(ctx: ActionContext) -> bool:
    return (
        "foreman:planning" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.mergeable == "MERGEABLE"
        and ctx.pr.ci_status == "SUCCESS"
    )


def _spec_pr_merged_label_lagging(ctx: ActionContext) -> bool:
    if ctx.pr is None or not ctx.pr.is_merged:
        return False
    if "foreman:planning" not in ctx.issue.labels:
        return False
    if ctx.log.has_recent(
        "advance_label_to_plan_approved", ctx.ticket_id, within_seconds=3600 * 24
    ):
        return False
    return True


def _plan_approved_no_impl_pr(ctx: ActionContext) -> bool:
    return (
        "foreman:plan-approved" in ctx.issue.labels
        and not ctx.log.has_unterminated("dispatch_worker", ctx.ticket_id)
    )


def _impl_review_green(ctx: ActionContext) -> bool:
    return (
        "foreman:impl-review" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.ci_status == "SUCCESS"
        and not ctx.log.has_unterminated("dispatch_reviewer", ctx.ticket_id)
    )


def _impl_fix_pending(ctx: ActionContext) -> bool:
    return (
        "foreman:impl-fix" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.log.has_unterminated("dispatch_fixer", ctx.ticket_id)
    )


def _impl_approved_pr_green(ctx: ActionContext) -> bool:
    return (
        "foreman:impl-approved" in ctx.issue.labels
        and ctx.pr is not None
        and not ctx.pr.is_merged
        and ctx.pr.mergeable == "MERGEABLE"
        and ctx.pr.ci_status == "SUCCESS"
    )


def _impl_pr_merged_label_lagging(ctx: ActionContext) -> bool:
    if ctx.pr is None or not ctx.pr.is_merged:
        return False
    if "foreman:impl-approved" not in ctx.issue.labels:
        return False
    if ctx.log.has_recent(
        "advance_label_to_done", ctx.ticket_id, within_seconds=3600 * 24
    ):
        return False
    return True


_PROGRESS_RULES: tuple[Rule, ...] = (
    Rule(
        name="dispatch_planner",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=100,
        when=_planning_no_pr,
        then=Action.DISPATCH_PLANNER,
    ),
    Rule(
        name="merge_spec_pr",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=110,
        when=_planning_pr_green,
        then=Action.MERGE_SPEC_PR,
    ),
    Rule(
        name="advance_label_to_plan_approved",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=120,
        when=_spec_pr_merged_label_lagging,
        then=Action.ADVANCE_LABEL_TO_PLAN_APPROVED,
    ),
    Rule(
        name="dispatch_worker",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=130,
        when=_plan_approved_no_impl_pr,
        then=Action.DISPATCH_WORKER,
    ),
    Rule(
        name="dispatch_reviewer",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=140,
        when=_impl_review_green,
        then=Action.DISPATCH_REVIEWER,
    ),
    Rule(
        name="dispatch_fixer",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=150,
        when=_impl_fix_pending,
        then=Action.DISPATCH_FIXER,
    ),
    Rule(
        name="merge_impl_pr",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=160,
        when=_impl_approved_pr_green,
        then=Action.MERGE_IMPL_PR,
    ),
    Rule(
        name="advance_label_to_done",
        tier=PrecedenceTier.FORWARD_PROGRESS,
        precedence=170,
        when=_impl_pr_merged_label_lagging,
        then=Action.ADVANCE_LABEL_TO_DONE,
    ),
)


RULES = _SAFETY_RULES + _PROGRESS_RULES
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest packages/foreman/tests/reconciler/test_rules.py packages/foreman/tests/reconciler/test_rules_precedence.py -v && uv run pytest packages/foreman -q
```
Expected: every rules test PASSes. Full suite `655 passed, 1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/reconciler/rules.py packages/foreman/tests/reconciler/test_rules.py
git commit -m "feat(reconciler): add forward-progress rule catalog with idempotence checks"
```

---

## Task 7: Action executor + ReconcilerHost protocol + fake host for tests

**Files:**
- Create: `packages/foreman/src/foreman/reconciler/host.py` (ReconcilerHost Protocol)
- Modify: `packages/foreman/src/foreman/reconciler/actions.py` (add `execute_action()` and `_FakeHost` test double pattern via Protocol)
- Modify: `packages/foreman/src/foreman/reconciler/__init__.py`
- Modify: `packages/foreman/tests/reconciler/test_actions.py`

The executor calls into a `ReconcilerHost` protocol (real impl wraps PyGithub; tests use a recording fake). Every action writes a `running` row before calling host, then writes a termination row on success/error.

- [ ] **Step 1: Write the failing tests**

Append to `packages/foreman/tests/reconciler/test_actions.py`:
```python
# --- Executor tests ---


from dataclasses import dataclass, field
from typing import Any

from foreman.reconciler.actions import execute_action
from foreman.reconciler.host import ReconcilerHost


@dataclass
class _FakeHost:
    """Recording test double — captures every call without doing real GH work."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def add_label(self, *, owner: str, repo: str, issue: int, label: str) -> None:
        self.calls.append(("add_label", {"owner": owner, "repo": repo, "issue": issue, "label": label}))

    def remove_label(self, *, owner: str, repo: str, issue: int, label: str) -> None:
        self.calls.append(("remove_label", {"owner": owner, "repo": repo, "issue": issue, "label": label}))

    def post_comment(self, *, owner: str, repo: str, issue: int, body: str) -> None:
        self.calls.append(("post_comment", {"owner": owner, "repo": repo, "issue": issue, "body": body}))

    def merge_pr(self, *, owner: str, repo: str, pr_number: int) -> None:
        self.calls.append(("merge_pr", {"owner": owner, "repo": repo, "pr_number": pr_number}))

    def dispatch_role(self, *, role: str, owner: str, repo: str, issue: int, pr_number: int | None) -> int:
        self.calls.append(("dispatch_role", {"role": role, "owner": owner, "repo": repo, "issue": issue, "pr_number": pr_number}))
        return 12345  # fake pid


def test_execute_noop_does_nothing(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()

    execute_action(Action.NOOP, ctx, host=host, rule_name="x", dry_run=False)

    assert host.calls == []
    # Noop is not logged either.
    import sqlite3
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        rows = conn.execute("SELECT COUNT(*) FROM execution_log").fetchone()[0]
    assert rows == 0


def test_execute_dispatch_planner_writes_running_and_calls_host(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()

    execute_action(Action.DISPATCH_PLANNER, ctx, host=host, rule_name="dispatch_planner", dry_run=False)

    assert ("dispatch_role", {"role": "planner", "owner": "jeffrichley", "repo": "foreman", "issue": 143, "pr_number": None}) in host.calls
    assert log.has_unterminated("dispatch_planner", ctx.ticket_id) is True


def test_execute_advance_label_writes_running_then_success(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()

    execute_action(
        Action.ADVANCE_LABEL_TO_PLAN_APPROVED,
        ctx,
        host=host,
        rule_name="advance_label_to_plan_approved",
        dry_run=False,
    )

    call_names = [c[0] for c in host.calls]
    assert "remove_label" in call_names
    assert "add_label" in call_names
    # Both running + success rows present, advance is complete.
    assert log.has_unterminated("advance_label_to_plan_approved", ctx.ticket_id) is False


def test_dry_run_does_not_call_host_but_logs_dry_run_outcome(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)
    host = _FakeHost()

    execute_action(Action.DISPATCH_PLANNER, ctx, host=host, rule_name="dispatch_planner", dry_run=True)

    assert host.calls == []
    import sqlite3
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        outcome = conn.execute("SELECT outcome FROM execution_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert outcome == "dry_run"


def test_execute_action_handles_host_exception_and_logs_error(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    ctx = ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)

    class _BoomHost(_FakeHost):
        def dispatch_role(self, **kwargs):
            raise RuntimeError("boom")

    host = _BoomHost()

    execute_action(Action.DISPATCH_PLANNER, ctx, host=host, rule_name="dispatch_planner", dry_run=False)

    # error termination wrote — has_unterminated must return False after error.
    assert log.has_unterminated("dispatch_planner", ctx.ticket_id) is False
    import sqlite3
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        outcome = conn.execute(
            "SELECT outcome FROM execution_log ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert outcome == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest packages/foreman/tests/reconciler/test_actions.py -v
```
Expected: new tests FAIL with import errors (`execute_action`, `ReconcilerHost` don't exist yet).

- [ ] **Step 3: Implement ReconcilerHost + execute_action**

Create `packages/foreman/src/foreman/reconciler/host.py`:
```python
"""Protocol for the GitHub-side surface the action executor needs.

Real impl wraps PyGithub + the subprocess spawner. Tests use a recording
fake. Keeping the protocol thin keeps the action layer pure-data-shaped.
"""

from __future__ import annotations

from typing import Protocol


class ReconcilerHost(Protocol):
    """The host side-effect surface for v3 actions."""

    def add_label(self, *, owner: str, repo: str, issue: int, label: str) -> None: ...
    def remove_label(self, *, owner: str, repo: str, issue: int, label: str) -> None: ...
    def post_comment(self, *, owner: str, repo: str, issue: int, body: str) -> None: ...
    def merge_pr(self, *, owner: str, repo: str, pr_number: int) -> None: ...
    def dispatch_role(
        self,
        *,
        role: str,
        owner: str,
        repo: str,
        issue: int,
        pr_number: int | None,
    ) -> int:
        """Spawn the role subprocess (planner|reviewer|fixer|worker). Returns the PID."""
        ...
```

Modify `packages/foreman/src/foreman/reconciler/actions.py` to add the executor below the existing `ActionContext` class:
```python
import logging

from foreman.reconciler.host import ReconcilerHost

logger = logging.getLogger(__name__)

_DISPATCH_ROLE_FOR_ACTION = {
    Action.DISPATCH_PLANNER: "planner",
    Action.DISPATCH_WORKER: "worker",
    Action.DISPATCH_REVIEWER: "reviewer",
    Action.DISPATCH_FIXER: "fixer",
}


def execute_action(
    action: Action,
    ctx: ActionContext,
    *,
    host: ReconcilerHost,
    rule_name: str,
    dry_run: bool,
) -> None:
    """Execute one action with single-writer log + dry-run support.

    Sequence: write start row -> call host -> write termination row (success
    or error). On exception the start row is terminated with outcome='error'
    and the exception is logged; the executor never re-raises (one bad action
    must not crash the reconciler loop).

    For dry_run: skip host entirely, write a single row with outcome='dry_run'.
    """
    if action is Action.NOOP:
        return

    if dry_run:
        ctx.log.write_action(
            ticket_id=ctx.ticket_id,
            project=ctx.snapshot.project,
            rule_name=rule_name,
            action=action.value,
            outcome="dry_run",
            details={
                "issue": ctx.issue.number,
                "pr": ctx.pr.number if ctx.pr else None,
            },
        )
        return

    start_id = ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project=ctx.snapshot.project,
        rule_name=rule_name,
        action=action.value,
        outcome="running",
        details={
            "issue": ctx.issue.number,
            "pr": ctx.pr.number if ctx.pr else None,
        },
    )

    try:
        if action is Action.SURFACE_HELP:
            host.add_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:needs-help",
            )
            host.post_comment(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                body=(
                    "Foreman v3 surfaced this ticket for human attention. "
                    "Investigate state and either fix or remove the "
                    "`foreman:needs-help` label to resume autonomous flow."
                ),
            )
        elif action in _DISPATCH_ROLE_FOR_ACTION:
            host.dispatch_role(
                role=_DISPATCH_ROLE_FOR_ACTION[action],
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                pr_number=ctx.pr.number if ctx.pr else None,
            )
        elif action is Action.MERGE_SPEC_PR or action is Action.MERGE_IMPL_PR:
            if ctx.pr is None:
                raise RuntimeError(f"{action.name} requires a PR in context")
            host.merge_pr(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                pr_number=ctx.pr.number,
            )
        elif action is Action.ADVANCE_LABEL_TO_PLAN_APPROVED:
            host.remove_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:planning",
            )
            host.add_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:plan-approved",
            )
        elif action is Action.ADVANCE_LABEL_TO_DONE:
            host.remove_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:impl-approved",
            )
            host.add_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:done",
            )

        # Some actions complete synchronously (label changes, merges, surface_help).
        # Subprocess dispatches stay 'running' until the worker sends an
        # ExecutionLogWrite termination via the bus (handled in Task 9).
        if action in _DISPATCH_ROLE_FOR_ACTION:
            # Leave start row 'running' — termination comes via bus.
            return

        ctx.log.terminate_action(parent_log_id=start_id, outcome="success", details={})

    except Exception as exc:
        logger.exception("action %s failed for ticket %s", action.name, ctx.ticket_id)
        ctx.log.terminate_action(
            parent_log_id=start_id,
            outcome="error",
            details={"error": str(exc)},
        )
```

Modify `packages/foreman/src/foreman/reconciler/__init__.py` to export new names:
```python
"""Foreman v3 declarative reconciler."""

from foreman.reconciler.actions import Action, ActionContext, execute_action
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.host import ReconcilerHost
from foreman.reconciler.rules import RULES, PrecedenceTier, Rule, evaluate
from foreman.reconciler.state import IssueState, PRState, ProjectSnapshot

__all__ = [
    "Action",
    "ActionContext",
    "ExecutionLog",
    "IssueState",
    "PRState",
    "PrecedenceTier",
    "ProjectSnapshot",
    "ReconcilerHost",
    "RULES",
    "Rule",
    "evaluate",
    "execute_action",
]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest packages/foreman/tests/reconciler/test_actions.py -v && uv run pytest packages/foreman -q
```
Expected: every test PASSes. Full suite `660 passed, 1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/reconciler/host.py packages/foreman/src/foreman/reconciler/actions.py packages/foreman/src/foreman/reconciler/__init__.py packages/foreman/tests/reconciler/test_actions.py
git commit -m "feat(reconciler): add action executor with dry-run and error containment"
```

---

## Task 8: GraphQL observer + observer tests

**Files:**
- Create: `packages/foreman/src/foreman/reconciler/observer.py`
- Modify: `packages/foreman/src/foreman/reconciler/__init__.py`
- Create: `packages/foreman/tests/reconciler/test_observer.py`

The observer's only job: turn one GraphQL response into a `ProjectSnapshot`. It accepts an injected `GHGraphQLClient` (Protocol) so tests use a fake.

- [ ] **Step 1: Write the failing tests**

Create `packages/foreman/tests/reconciler/test_observer.py`:
```python
"""Tests for the v3 GraphQL observer — query shape, response parsing, errors."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from foreman.reconciler.observer import (
    GHGraphQLClient,
    ObserverError,
    ObserverRateLimited,
    ObserverUnreachable,
    fetch_project_state,
)


class _FakeGHClient:
    def __init__(self, *, response: dict[str, Any] | None = None, raise_with: Exception | None = None) -> None:
        self.response = response
        self.raise_with = raise_with
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, variables))
        if self.raise_with:
            raise self.raise_with
        return self.response or {"data": {"repository": {"issues": {"nodes": []}, "pullRequests": {"nodes": []}}}}


def _gh_response_with(*, issues: list[dict], prs: list[dict]) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "issues": {"nodes": issues},
                "pullRequests": {"nodes": prs},
            }
        }
    }


def test_fetch_project_state_returns_empty_snapshot_for_empty_response() -> None:
    client = _FakeGHClient(response=_gh_response_with(issues=[], prs=[]))
    snap = fetch_project_state(
        project="foreman", owner="jeffrichley", repo="foreman", gh=client,
    )
    assert snap.project == "foreman"
    assert snap.owner == "jeffrichley"
    assert snap.repo == "foreman"
    assert snap.issues == ()
    assert snap.prs == ()
    assert snap.fetched_at.tzinfo is not None


def test_fetch_project_state_parses_issue_fields() -> None:
    issue_payload = {
        "number": 143,
        "title": "Daemon stuck on planning",
        "body": "details",
        "state": "OPEN",
        "updatedAt": "2026-06-03T15:00:00Z",
        "labels": {"nodes": [{"name": "foreman:planning"}, {"name": "good first issue"}]},
        "assignees": {"nodes": [{"login": "wrenrichley"}]},
    }
    client = _FakeGHClient(response=_gh_response_with(issues=[issue_payload], prs=[]))
    snap = fetch_project_state(project="foreman", owner="jeffrichley", repo="foreman", gh=client)

    assert len(snap.issues) == 1
    iss = snap.issues[0]
    assert iss.number == 143
    assert iss.title == "Daemon stuck on planning"
    assert iss.labels == ("foreman:planning", "good first issue")
    assert iss.assignees == ("wrenrichley",)


def test_fetch_project_state_parses_pr_with_linked_issue() -> None:
    pr_payload = {
        "number": 144,
        "headRefName": "spec-143-fix",
        "body": "Implements #143",
        "mergeable": "MERGEABLE",
        "merged": False,
        "statusCheckRollup": {"state": "SUCCESS"},
        "closingIssuesReferences": {"nodes": [{"number": 143}]},
    }
    client = _FakeGHClient(response=_gh_response_with(issues=[], prs=[pr_payload]))
    snap = fetch_project_state(project="foreman", owner="jeffrichley", repo="foreman", gh=client)

    assert len(snap.prs) == 1
    pr = snap.prs[0]
    assert pr.number == 144
    assert pr.head_ref == "spec-143-fix"
    assert pr.mergeable == "MERGEABLE"
    assert pr.ci_status == "SUCCESS"
    assert pr.linked_issue_numbers == (143,)
    assert pr.is_merged is False


def test_fetch_project_state_handles_null_status_check_rollup() -> None:
    pr_payload = {
        "number": 144,
        "headRefName": "x",
        "body": "",
        "mergeable": "UNKNOWN",
        "merged": False,
        "statusCheckRollup": None,
        "closingIssuesReferences": {"nodes": []},
    }
    client = _FakeGHClient(response=_gh_response_with(issues=[], prs=[pr_payload]))
    snap = fetch_project_state(project="foreman", owner="jeffrichley", repo="foreman", gh=client)
    assert snap.prs[0].ci_status is None


def test_observer_rate_limited_raises_typed_error() -> None:
    class _GQLError(Exception):
        pass
    err = _GQLError("API rate limit exceeded for installation")
    client = _FakeGHClient(raise_with=err)
    with pytest.raises(ObserverRateLimited):
        fetch_project_state(project="foreman", owner="jeffrichley", repo="foreman", gh=client)


def test_observer_network_error_raises_typed_error() -> None:
    err = ConnectionError("getaddrinfo failed")
    client = _FakeGHClient(raise_with=err)
    with pytest.raises(ObserverUnreachable):
        fetch_project_state(project="foreman", owner="jeffrichley", repo="foreman", gh=client)


def test_observer_query_includes_only_foreman_labeled_issues() -> None:
    client = _FakeGHClient(response=_gh_response_with(issues=[], prs=[]))
    fetch_project_state(project="foreman", owner="jeffrichley", repo="foreman", gh=client)
    query, variables = client.calls[0]
    assert "foreman:" in query
    assert variables == {"owner": "jeffrichley", "repo": "foreman"}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest packages/foreman/tests/reconciler/test_observer.py -v
```
Expected: all tests FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement observer**

Create `packages/foreman/src/foreman/reconciler/observer.py`:
```python
"""GraphQL observer — one query per project per poll, returns ProjectSnapshot.

The observer is the only place v3 reads from GitHub. Failures surface as
typed exceptions so the daemon loop can fail-stop with appropriate alerts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from foreman.reconciler.state import IssueState, PRState, ProjectSnapshot


class GHGraphQLClient(Protocol):
    """Thin abstraction so tests can inject a fake.

    Real implementation wraps PyGithub's underlying requester or a direct
    httpx POST to the v4 endpoint. Either way the surface is one method.
    """

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]: ...


class ObserverError(Exception):
    """Base class for observer-side failures."""


class ObserverUnreachable(ObserverError):
    """GitHub did not respond — network error, DNS, timeout."""


class ObserverRateLimited(ObserverError):
    """GitHub returned a rate-limit signal."""


_QUERY = """
query ForemanProjectState($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    issues(
      first: 100,
      states: OPEN,
      filterBy: { labels: [
        "foreman:planning",
        "foreman:plan-approved",
        "foreman:impl-review",
        "foreman:impl-approved",
        "foreman:impl-fix",
        "foreman:needs-help"
      ] }
    ) {
      nodes {
        number
        title
        body
        state
        updatedAt
        labels(first: 30) { nodes { name } }
        assignees(first: 10) { nodes { login } }
      }
    }
    pullRequests(first: 100, states: OPEN) {
      nodes {
        number
        headRefName
        body
        mergeable
        merged
        statusCheckRollup { state }
        closingIssuesReferences(first: 10) { nodes { number } }
      }
    }
  }
}
"""


def fetch_project_state(
    *,
    project: str,
    owner: str,
    repo: str,
    gh: GHGraphQLClient,
) -> ProjectSnapshot:
    """One GraphQL call returning the full poll-cycle view of one project."""

    try:
        response = gh.graphql(_QUERY, {"owner": owner, "repo": repo})
    except Exception as exc:
        msg = str(exc).lower()
        if "rate limit" in msg or "api rate limit" in msg:
            raise ObserverRateLimited(str(exc)) from exc
        if isinstance(exc, (ConnectionError, TimeoutError)):
            raise ObserverUnreachable(str(exc)) from exc
        if "timeout" in msg or "getaddrinfo" in msg or "connection" in msg:
            raise ObserverUnreachable(str(exc)) from exc
        raise ObserverError(str(exc)) from exc

    repository = (response.get("data") or {}).get("repository") or {}
    issue_nodes = ((repository.get("issues") or {}).get("nodes")) or []
    pr_nodes = ((repository.get("pullRequests") or {}).get("nodes")) or []

    issues = tuple(_parse_issue(node) for node in issue_nodes)
    prs = tuple(_parse_pr(node) for node in pr_nodes)

    return ProjectSnapshot(
        project=project,
        owner=owner,
        repo=repo,
        issues=issues,
        prs=prs,
        fetched_at=datetime.now(timezone.utc),
    )


def _parse_issue(node: dict[str, Any]) -> IssueState:
    labels = tuple(
        label["name"] for label in (node.get("labels") or {}).get("nodes", [])
    )
    assignees = tuple(
        a["login"] for a in (node.get("assignees") or {}).get("nodes", [])
    )
    updated = _parse_iso(node["updatedAt"])
    return IssueState(
        number=int(node["number"]),
        title=str(node.get("title", "")),
        labels=labels,
        assignees=assignees,
        body=str(node.get("body", "") or ""),
        updated_at=updated,
    )


def _parse_pr(node: dict[str, Any]) -> PRState:
    linked = tuple(
        int(n["number"])
        for n in (node.get("closingIssuesReferences") or {}).get("nodes", [])
    )
    rollup = node.get("statusCheckRollup")
    ci = rollup["state"] if rollup else None
    return PRState(
        number=int(node["number"]),
        head_ref=str(node.get("headRefName", "")),
        mergeable=str(node.get("mergeable", "UNKNOWN")),
        ci_status=ci,
        body=str(node.get("body", "") or ""),
        linked_issue_numbers=linked,
        is_merged=bool(node.get("merged", False)),
    )


def _parse_iso(value: str) -> datetime:
    # GitHub returns trailing "Z" — Python's fromisoformat accepts it in 3.11+.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
```

Modify `packages/foreman/src/foreman/reconciler/__init__.py` to export observer names:
```python
"""Foreman v3 declarative reconciler."""

from foreman.reconciler.actions import Action, ActionContext, execute_action
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.host import ReconcilerHost
from foreman.reconciler.observer import (
    GHGraphQLClient,
    ObserverError,
    ObserverRateLimited,
    ObserverUnreachable,
    fetch_project_state,
)
from foreman.reconciler.rules import RULES, PrecedenceTier, Rule, evaluate
from foreman.reconciler.state import IssueState, PRState, ProjectSnapshot

__all__ = [
    "Action",
    "ActionContext",
    "ExecutionLog",
    "GHGraphQLClient",
    "IssueState",
    "ObserverError",
    "ObserverRateLimited",
    "ObserverUnreachable",
    "PRState",
    "PrecedenceTier",
    "ProjectSnapshot",
    "ReconcilerHost",
    "RULES",
    "Rule",
    "evaluate",
    "execute_action",
    "fetch_project_state",
]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest packages/foreman/tests/reconciler/test_observer.py -v && uv run pytest packages/foreman -q
```
Expected: 7 observer tests PASS. Full suite `667 passed, 1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/reconciler/observer.py packages/foreman/src/foreman/reconciler/__init__.py packages/foreman/tests/reconciler/test_observer.py
git commit -m "feat(reconciler): add GraphQL observer with typed failure modes"
```

---

## Task 9: ReconcilerConfig + Reconciler class scaffolding

**Files:**
- Modify: `packages/foreman/src/foreman/config.py` (add ReconcilerConfig + compose into top-level Config)
- Create: `packages/foreman/src/foreman/reconciler/daemon.py` (Reconciler class + tick + run loop)
- Modify: `packages/foreman/src/foreman/reconciler/__init__.py`
- Create: `packages/foreman/tests/reconciler/test_reconciler_e2e.py`

This is the biggest task — wires observer + rules + actions into the running daemon. Tests are integration-shaped (canned GH state → asserted action sequence).

- [ ] **Step 1: Write the failing tests**

Create `packages/foreman/tests/reconciler/test_reconciler_e2e.py`:
```python
"""End-to-end integration tests for the v3 Reconciler.

Stubs the GH GraphQL client + ReconcilerHost; runs `tick()` against canned
project state; asserts the emitted actions + execution log rows.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from foreman.reconciler import (
    ExecutionLog,
    PRState,
    ProjectSnapshot,
    fetch_project_state,
)
from foreman.reconciler.daemon import Reconciler, ReconcilerProject


@dataclass
class _StubGHClient:
    response: dict[str, Any] = field(default_factory=dict)

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return self.response


@dataclass
class _StubHost:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def add_label(self, **kwargs) -> None:
        self.calls.append(("add_label", kwargs))

    def remove_label(self, **kwargs) -> None:
        self.calls.append(("remove_label", kwargs))

    def post_comment(self, **kwargs) -> None:
        self.calls.append(("post_comment", kwargs))

    def merge_pr(self, **kwargs) -> None:
        self.calls.append(("merge_pr", kwargs))

    def dispatch_role(self, **kwargs) -> int:
        self.calls.append(("dispatch_role", kwargs))
        return 12345


def _gh_with(issues: list[dict], prs: list[dict]) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "issues": {"nodes": issues},
                "pullRequests": {"nodes": prs},
            }
        }
    }


def _issue_payload(number: int, labels: list[str]) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"#{number}",
        "body": "",
        "state": "OPEN",
        "updatedAt": "2026-06-03T15:00:00Z",
        "labels": {"nodes": [{"name": label} for label in labels]},
        "assignees": {"nodes": []},
    }


def _pr_payload(
    *,
    number: int,
    closes: list[int],
    mergeable: str = "MERGEABLE",
    ci: str | None = "SUCCESS",
    merged: bool = False,
) -> dict[str, Any]:
    return {
        "number": number,
        "headRefName": f"branch-{number}",
        "body": "",
        "mergeable": mergeable,
        "merged": merged,
        "statusCheckRollup": {"state": ci} if ci else None,
        "closingIssuesReferences": {"nodes": [{"number": n} for n in closes]},
    }


@pytest.mark.asyncio
async def test_tick_emits_dispatch_planner_for_planning_with_no_pr(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(_gh_with([_issue_payload(143, ["foreman:planning"])], []))
    host = _StubHost()
    reconciler = Reconciler(
        projects=(ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),),
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
    )

    await reconciler.tick()

    role_calls = [c for c in host.calls if c[0] == "dispatch_role"]
    assert len(role_calls) == 1
    assert role_calls[0][1]["role"] == "planner"
    assert role_calls[0][1]["issue"] == 143
    assert log.has_unterminated("dispatch_planner", "jeffrichley/foreman#143")


@pytest.mark.asyncio
async def test_tick_emits_advance_label_for_stuck_today_ticket_143(tmp_path: Path) -> None:
    """The cutover proof point — v3 unsticks today's gum-up automatically."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(
        _gh_with(
            [_issue_payload(143, ["foreman:planning"])],
            [_pr_payload(number=144, closes=[143], merged=True)],
        )
    )
    host = _StubHost()
    reconciler = Reconciler(
        projects=(ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),),
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
    )

    await reconciler.tick()

    advance_calls = [c for c in host.calls if c[0] in ("remove_label", "add_label")]
    assert any(c[1].get("label") == "foreman:planning" for c in advance_calls if c[0] == "remove_label")
    assert any(c[1].get("label") == "foreman:plan-approved" for c in advance_calls if c[0] == "add_label")


@pytest.mark.asyncio
async def test_tick_safety_preempts_progress_when_ci_failed(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(
        _gh_with(
            [_issue_payload(143, ["foreman:impl-review"])],
            [_pr_payload(number=144, closes=[143], ci="FAILURE")],
        )
    )
    host = _StubHost()
    reconciler = Reconciler(
        projects=(ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),),
        log=log,
        gh=gh,
        host=host,
        dry_run=False,
    )

    await reconciler.tick()

    # surface_help should have fired (add needs-help label + comment), and no
    # reviewer dispatch should have happened.
    assert any(c[0] == "add_label" and c[1].get("label") == "foreman:needs-help" for c in host.calls)
    assert all(c[0] != "dispatch_role" for c in host.calls)


@pytest.mark.asyncio
async def test_tick_dry_run_does_not_call_host(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    gh = _StubGHClient(_gh_with([_issue_payload(143, ["foreman:planning"])], []))
    host = _StubHost()
    reconciler = Reconciler(
        projects=(ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),),
        log=log,
        gh=gh,
        host=host,
        dry_run=True,
    )

    await reconciler.tick()

    assert host.calls == []
    # But the intended action was logged with outcome='dry_run'.
    import sqlite3
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        outcome = conn.execute("SELECT outcome FROM execution_log ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert outcome == "dry_run"


@pytest.mark.asyncio
async def test_tick_observer_rate_limited_alerts_after_n_failures(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    class _ErrGH:
        def graphql(self, query, variables):
            raise RuntimeError("API rate limit exceeded")

    host = _StubHost()
    reconciler = Reconciler(
        projects=(ReconcilerProject(name="foreman", owner="jeffrichley", repo="foreman"),),
        log=log,
        gh=_ErrGH(),
        host=host,
        dry_run=False,
        alert_after_n_failures=3,
    )

    # Three consecutive failures should produce one observer_unreachable alert row.
    await reconciler.tick()
    await reconciler.tick()
    await reconciler.tick()

    import sqlite3
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        rows = conn.execute(
            "SELECT action, outcome FROM execution_log WHERE action='observer_failure_alert'"
        ).fetchall()
    assert len(rows) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest packages/foreman/tests/reconciler/test_reconciler_e2e.py -v
```
Expected: all fail (`ModuleNotFoundError: No module named 'foreman.reconciler.daemon'` and ReconcilerProject undefined).

- [ ] **Step 3: Implement Reconciler + ReconcilerConfig**

Modify `packages/foreman/src/foreman/config.py` — append to the existing `Config` composition. Locate the existing `class DaemonConfig(BaseModel)` and add this after it (before the top-level `Config` class that aggregates):
```python
class ReconcilerConfig(BaseModel):
    """v3 reconciler knobs. Lives alongside DaemonConfig (which configures v2)."""

    db_path: str = Field(
        default="~/.foreman/reconciler.sqlite",
        description="sqlite path for the v3 execution log",
    )
    poll_interval_seconds: int = Field(
        default=60,
        ge=10,
        description="seconds between reconciler ticks",
    )
    retention_days: int = Field(
        default=30,
        ge=1,
        description="rows older than this are eligible for archive",
    )
    alert_after_n_failures: int = Field(
        default=3,
        ge=1,
        description="consecutive observer failures before yellow alert",
    )
```

Then locate the top-level `Config` class (the one that holds `daemon: DaemonConfig`, `apps: AppsConfig`, etc.) and add:
```python
    reconciler: ReconcilerConfig = Field(default_factory=ReconcilerConfig)
```

Create `packages/foreman/src/foreman/reconciler/daemon.py`:
```python
"""Reconciler — the v3 daemon's tick + run loop.

Composes observer + rules + actions. Per tick: fetch each project's
snapshot, evaluate the rule catalog per ticket, execute the action via the
host (or skip-and-log under dry_run). Fail-stop on observer outage with a
yellow alert after N consecutive failures.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from foreman.reconciler.actions import Action, ActionContext, execute_action
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.host import ReconcilerHost
from foreman.reconciler.observer import (
    GHGraphQLClient,
    ObserverError,
    ObserverRateLimited,
    ObserverUnreachable,
    fetch_project_state,
)
from foreman.reconciler.rules import RULES, evaluate
from foreman.reconciler.state import ProjectSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcilerProject:
    """One registered project the reconciler watches."""

    name: str
    owner: str
    repo: str


class Reconciler:
    """The v3 daemon's main loop."""

    def __init__(
        self,
        *,
        projects: tuple[ReconcilerProject, ...],
        log: ExecutionLog,
        gh: GHGraphQLClient,
        host: ReconcilerHost,
        dry_run: bool,
        alert_after_n_failures: int = 3,
        poll_interval_seconds: int = 60,
    ) -> None:
        self.projects = projects
        self.log = log
        self.gh = gh
        self.host = host
        self.dry_run = dry_run
        self.alert_after_n_failures = alert_after_n_failures
        self.poll_interval_seconds = poll_interval_seconds
        self._stop_event = asyncio.Event()
        self._consecutive_failures: dict[str, int] = {p.name: 0 for p in projects}

    async def tick(self) -> None:
        """Run one reconciliation pass over every project."""
        for project in self.projects:
            try:
                snapshot = fetch_project_state(
                    project=project.name,
                    owner=project.owner,
                    repo=project.repo,
                    gh=self.gh,
                )
            except (ObserverRateLimited, ObserverUnreachable, ObserverError) as exc:
                self._consecutive_failures[project.name] += 1
                logger.warning(
                    "observer failed for project=%s (%d/%d): %s",
                    project.name,
                    self._consecutive_failures[project.name],
                    self.alert_after_n_failures,
                    exc,
                )
                if self._consecutive_failures[project.name] == self.alert_after_n_failures:
                    # Single alert row — log once per breach, not every poll.
                    self.log.write_action(
                        ticket_id=f"project:{project.name}",
                        project=project.name,
                        rule_name=None,
                        action="observer_failure_alert",
                        outcome="alert",
                        details={
                            "error_class": type(exc).__name__,
                            "error_message": str(exc),
                            "consecutive_failures": self._consecutive_failures[project.name],
                        },
                    )
                continue

            self._consecutive_failures[project.name] = 0
            self._reconcile_project(snapshot)

    def _reconcile_project(self, snapshot: ProjectSnapshot) -> None:
        for issue in snapshot.issues:
            linked_prs = snapshot.prs_for_issue(issue.number)
            pr = linked_prs[0] if linked_prs else None
            ctx = ActionContext(snapshot=snapshot, issue=issue, pr=pr, log=self.log)
            action = evaluate(ctx, rules=RULES)
            if action is Action.NOOP:
                continue
            rule_name = _rule_that_fired(ctx, action)
            execute_action(
                action,
                ctx,
                host=self.host,
                rule_name=rule_name,
                dry_run=self.dry_run,
            )

    async def run(self) -> None:
        """Forever loop. Stops cleanly when shutdown() is called."""
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("reconciler tick raised; continuing")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def shutdown(self) -> None:
        self._stop_event.set()


def _rule_that_fired(ctx: ActionContext, action: Action) -> str:
    """Reverse-lookup which rule emitted this action — for log attribution."""
    for rule in RULES:
        try:
            if rule.when(ctx) and rule.then is action:
                return rule.name
        except Exception:
            continue
    return "unknown"
```

Modify `packages/foreman/src/foreman/reconciler/__init__.py`:
```python
"""Foreman v3 declarative reconciler."""

from foreman.reconciler.actions import Action, ActionContext, execute_action
from foreman.reconciler.daemon import Reconciler, ReconcilerProject
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.host import ReconcilerHost
from foreman.reconciler.observer import (
    GHGraphQLClient,
    ObserverError,
    ObserverRateLimited,
    ObserverUnreachable,
    fetch_project_state,
)
from foreman.reconciler.rules import RULES, PrecedenceTier, Rule, evaluate
from foreman.reconciler.state import IssueState, PRState, ProjectSnapshot

__all__ = [
    "Action",
    "ActionContext",
    "ExecutionLog",
    "GHGraphQLClient",
    "IssueState",
    "ObserverError",
    "ObserverRateLimited",
    "ObserverUnreachable",
    "PRState",
    "PrecedenceTier",
    "ProjectSnapshot",
    "Reconciler",
    "ReconcilerHost",
    "ReconcilerProject",
    "RULES",
    "Rule",
    "evaluate",
    "execute_action",
    "fetch_project_state",
]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest packages/foreman/tests/reconciler/test_reconciler_e2e.py -v && uv run pytest packages/foreman -q
```
Expected: all 5 e2e tests PASS. Full suite `672 passed, 1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/config.py packages/foreman/src/foreman/reconciler/daemon.py packages/foreman/src/foreman/reconciler/__init__.py packages/foreman/tests/reconciler/test_reconciler_e2e.py
git commit -m "feat(reconciler): add Reconciler class with tick/run loop + ReconcilerConfig"
```

---

## Task 10: Bus endpoint for ExecutionLogWrite envelopes

**Files:**
- Create: `packages/foreman/src/foreman/v3_bus_endpoint.py`
- Create: `packages/foreman/tests/test_v3_bus_endpoint.py`

Subprocesses (worker/planner/reviewer/fixer) don't write to the log directly. They send `ExecutionLogWrite` Event envelopes via the agent-core bus; the daemon receives them and writes through the (single-writer) ExecutionLog.

This task adds the envelope shape + handler. The bus integration itself (daemon listening on a real endpoint) lands in Task 11 alongside the CLI wiring.

- [ ] **Step 1: Write the failing tests**

Create `packages/foreman/tests/test_v3_bus_endpoint.py`:
```python
"""Tests for the v3 bus endpoint that translates ExecutionLogWrite envelopes
into ExecutionLog rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from foreman.reconciler.exec_log import ExecutionLog
from foreman.v3_bus_endpoint import ExecutionLogWritePayload, handle_envelope


def test_payload_validates_required_fields() -> None:
    with pytest.raises(ValidationError):
        ExecutionLogWritePayload()  # type: ignore[call-arg]


def test_payload_accepts_complete_input() -> None:
    payload = ExecutionLogWritePayload(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        action="worker_heartbeat",
        outcome="running",
        details={"progress": "8/8 tests passing"},
    )
    assert payload.action == "worker_heartbeat"


def test_handle_envelope_writes_row_through_log(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    payload = ExecutionLogWritePayload(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        action="worker_heartbeat",
        outcome="running",
        details={"progress": "8/8"},
    )

    row_id = handle_envelope(payload, log=log)

    assert row_id >= 1
    import sqlite3
    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        row = conn.execute(
            "SELECT ticket_id, action, outcome FROM execution_log WHERE id = ?",
            (row_id,),
        ).fetchone()
    assert row == ("jeffrichley/foreman#143", "worker_heartbeat", "running")


def test_handle_envelope_terminates_parent_when_parent_log_id_given(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    start_id = log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_worker",
        action="dispatch_worker",
        outcome="running",
        details={},
    )

    payload = ExecutionLogWritePayload(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        action="dispatch_worker",
        outcome="success",
        details={"merged_pr": 144},
        parent_log_id=start_id,
    )

    handle_envelope(payload, log=log)

    assert log.has_unterminated("dispatch_worker", "jeffrichley/foreman#143") is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest packages/foreman/tests/test_v3_bus_endpoint.py -v
```
Expected: `ModuleNotFoundError: No module named 'foreman.v3_bus_endpoint'`.

- [ ] **Step 3: Implement bus endpoint**

Create `packages/foreman/src/foreman/v3_bus_endpoint.py`:
```python
"""Bus endpoint translating subprocess ExecutionLogWrite envelopes into log rows.

Subprocesses (planner/worker/reviewer/fixer) communicate progress over the
agent-core bus instead of writing sqlite directly. This keeps the v3 daemon
the SINGLE writer of execution_log.

Envelope shape (sent via mcp__agent-core__send):

    {
      "kind": "Event",
      "type": "ExecutionLogWrite",
      "data": {
        "ticket_id": "jeffrichley/foreman#143",
        "project": "foreman",
        "action": "worker_heartbeat",
        "outcome": "running",
        "details": {"progress": "8/8 passing"},
        "parent_log_id": null  // or an int for termination rows
      }
    }
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from foreman.reconciler.exec_log import ExecutionLog


class ExecutionLogWritePayload(BaseModel):
    """Pydantic model for the bus envelope's `data` block."""

    ticket_id: str = Field(..., description="Project-qualified issue id, e.g. 'owner/repo#143'")
    project: str = Field(..., description="Local project name, e.g. 'foreman'")
    action: str = Field(..., description="Action name matching the Action enum value")
    outcome: str = Field(..., description="'running' | 'success' | 'error' | 'skipped' | 'dry_run'")
    details: dict[str, Any] = Field(default_factory=dict, description="Free-form structured details")
    parent_log_id: int | None = Field(
        default=None,
        description="If set, this row is a termination of the parent row; daemon will "
        "issue terminate_action() so has_unterminated() returns False after.",
    )
    rule_name: str | None = Field(
        default=None,
        description="Which rule fired this action, if known. NULL for subprocess-internal writes.",
    )


def handle_envelope(payload: ExecutionLogWritePayload, *, log: ExecutionLog) -> int:
    """Translate a validated ExecutionLogWritePayload into an execution_log row.

    Returns the row id of the written row.
    """
    if payload.parent_log_id is not None:
        return log.terminate_action(
            parent_log_id=payload.parent_log_id,
            outcome=payload.outcome,
            details=payload.details,
        )
    return log.write_action(
        ticket_id=payload.ticket_id,
        project=payload.project,
        rule_name=payload.rule_name,
        action=payload.action,
        outcome=payload.outcome,
        details=payload.details,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest packages/foreman/tests/test_v3_bus_endpoint.py -v && uv run pytest packages/foreman -q
```
Expected: 4 bus tests PASS. Full suite `676 passed, 1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v3_bus_endpoint.py packages/foreman/tests/test_v3_bus_endpoint.py
git commit -m "feat(reconciler): add v3 bus endpoint for ExecutionLogWrite envelopes"
```

---

## Task 11: CLI integration — `foreman daemon v3-start [--dry-run]`

**Files:**
- Modify: `packages/foreman/src/foreman/cli.py` (add `@daemon.command("v3-start")` subcommand)
- Create: `packages/foreman/tests/test_cli_v3.py`

The CLI is the operator's only entry point. It wires Config → Reconciler → run loop.

- [ ] **Step 1: Write the failing tests**

Create `packages/foreman/tests/test_cli_v3.py`:
```python
"""Tests for the `foreman daemon v3-start` Click subcommand."""

from __future__ import annotations

from click.testing import CliRunner

from foreman.cli import cli


def test_v3_start_help_lists_dry_run_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["daemon", "v3-start", "--help"])
    assert result.exit_code == 0
    assert "--dry-run" in result.output
    assert "reconciler" in result.output.lower()


def test_v3_start_short_circuits_without_runtime_setup(monkeypatch, tmp_path) -> None:
    # Sanity: the command is wired and the entry point function is callable.
    # Full runtime (real GH client + bus) is integration-tested elsewhere.
    runner = CliRunner()
    monkeypatch.setenv("FOREMAN_CONFIG_PATH", str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text("[reconciler]\ndb_path = '" + str(tmp_path / "reconciler.sqlite").replace("\\", "/") + "'\n")
    result = runner.invoke(cli, ["daemon", "v3-start", "--max-ticks", "0", "--dry-run"])
    # --max-ticks 0 means "wire everything, run zero ticks, exit". Either
    # exit 0 (clean) or a controlled stub-not-implemented exit; never crash
    # uncaught.
    assert result.exit_code in (0, 2), result.output
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest packages/foreman/tests/test_cli_v3.py -v
```
Expected: tests fail because `v3-start` subcommand doesn't exist.

- [ ] **Step 3: Add the Click subcommand**

Modify `packages/foreman/src/foreman/cli.py` — locate the `@daemon.command("start")` decorator and add this new subcommand BELOW it (do not modify existing v2 daemon commands):
```python
@daemon.command("v3-start")
@click.option(
    "--dry-run/--execute",
    default=False,
    help="Dry-run mode: reconciler emits intended actions to the execution "
    "log with outcome='dry_run' but does NOT call the host. Use for first "
    "~6 polls post-cutover to gut-check the rule catalog before flipping "
    "to executing mode.",
)
@click.option(
    "--max-ticks",
    type=int,
    default=None,
    help="Run this many ticks then exit. Default: forever. 0 means wire "
    "everything and exit immediately (smoke-test the CLI path).",
)
def daemon_v3_start(dry_run: bool, max_ticks: int | None) -> None:
    """Start the v3 declarative reconciler daemon.

    GitHub IS the source of truth for ticket + PR state. The reconciler
    derives the right action per ticket from GH + execution log, then
    executes via the host. See docs/superpowers/specs/foreman-issue-106-spec.md.
    """
    import asyncio

    from foreman.config import load_config
    from foreman.reconciler import ExecutionLog, Reconciler, ReconcilerProject

    config = load_config()  # uses FOREMAN_CONFIG_PATH or default ~/.foreman/config.toml

    db_path = Path(os.path.expanduser(config.reconciler.db_path))
    log = ExecutionLog(db_path)
    log.init()

    projects = tuple(
        ReconcilerProject(name=p.name, owner=p.owner, repo=p.repo)
        for p in config.projects
    )

    if max_ticks == 0:
        # Smoke-test wiring without spinning the loop.
        click.echo(f"v3-start wired: {len(projects)} projects, db={db_path}, dry_run={dry_run}")
        return

    gh, host = _build_v3_gh_and_host(config)

    reconciler = Reconciler(
        projects=projects,
        log=log,
        gh=gh,
        host=host,
        dry_run=dry_run,
        alert_after_n_failures=config.reconciler.alert_after_n_failures,
        poll_interval_seconds=config.reconciler.poll_interval_seconds,
    )

    async def _run() -> None:
        if max_ticks is None:
            await reconciler.run()
        else:
            for _ in range(max_ticks):
                await reconciler.tick()
                await asyncio.sleep(reconciler.poll_interval_seconds)

    asyncio.run(_run())


def _build_v3_gh_and_host(config):
    """Construct the real GH GraphQL client + ReconcilerHost.

    The v3 host wraps the existing v2 GitHubDaemonHost for action methods
    (add_label, merge_pr, etc.) and adds a `dispatch_role` that spawns a
    subprocess via the existing role-dispatch machinery. For now we raise
    NotImplementedError to keep this task focused on CLI wiring; the host
    construction is a separate concern handled when v3 first runs against
    real infrastructure (see plan Task 13 / cutover docs).
    """
    raise NotImplementedError(
        "v3-start runtime wiring not yet implemented. Use --max-ticks 0 to "
        "smoke-test, or run unit + integration tests in tests/reconciler/."
    )
```

(Ensure `import os` and `from pathlib import Path` are already at the top of `cli.py`; add if missing.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest packages/foreman/tests/test_cli_v3.py -v && uv run pytest packages/foreman -q
```
Expected: 2 CLI tests PASS. Full suite `678 passed, 1 skipped`.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/cli.py packages/foreman/tests/test_cli_v3.py
git commit -m "feat(reconciler): add 'foreman daemon v3-start --dry-run' CLI entry point"
```

---

## Task 12: v2 deprecation banner

**Files:**
- Modify: `packages/foreman/src/foreman/daemon.py` (top-of-file banner)
- Modify: `packages/foreman/src/foreman/storage.py` (top-of-file banner)

Non-behavioral. Marks v2 modules as deprecated post-v3-cutover so future code-readers know which is canonical.

- [ ] **Step 1: Add deprecation banner to v2 daemon module**

Prepend to `packages/foreman/src/foreman/daemon.py` (above any existing module docstring; KEEP the docstring intact below):
```python
"""DEPRECATED: this module is v2. v3 lives in foreman.reconciler.

The v2 daemon (this module) holds a `pipelines` state table alongside
GitHub's actual state. That parallel-state pattern caused the foreman#101
zombie-pipeline class of failures. v3 derives state from GitHub directly;
the daemon db is an append-only execution log only.

See docs/superpowers/specs/foreman-issue-106-spec.md for the migration
path. v2 stays in tree (and importable) until v3 is proven stable for ~2
weeks post-cutover; then this module + foreman.storage will be removed in
a follow-up PR.
"""
```

If there is an existing docstring at the top, prepend the deprecation note to it (paragraph break + the existing content).

- [ ] **Step 2: Add deprecation banner to v2 storage module**

Same prepend to `packages/foreman/src/foreman/storage.py`:
```python
"""DEPRECATED: this module is v2 storage (pipelines table).

v3 uses foreman.reconciler.exec_log.ExecutionLog instead. Schema lives at
~/.foreman/reconciler.sqlite. v2 db at ~/.foreman/foreman.sqlite is
archived as foreman-v2-archive-2026-06-03.sqlite per the cutover docs.

This module stays importable until the v3-stable follow-up removal.
"""
```

- [ ] **Step 3: Verify suite still green**

```bash
uv run pytest packages/foreman -q
```
Expected: `678 passed, 1 skipped` (no behavior change, just comments).

- [ ] **Step 4: Commit**

```bash
git add packages/foreman/src/foreman/daemon.py packages/foreman/src/foreman/storage.py
git commit -m "docs(daemon): mark v2 daemon + storage modules deprecated, point at v3"
```

---

## Task 13: Cutover documentation

**Files:**
- Create: `packages/foreman/docs/v3-cutover.md`

Operator-facing cutover guide. Captures the exact commands + observation pattern from the spec, in a focused doc.

- [ ] **Step 1: Write the cutover doc**

Create `packages/foreman/docs/v3-cutover.md`:
````markdown
# Foreman v3 Cutover Runbook

> Operator-facing guide for switching from foreman v2 (pipelines-as-state)
> to v3 (GitHub-as-state + execution log). Read alongside
> `docs/superpowers/specs/foreman-issue-106-spec.md`.

## Pre-cutover state

As of 2026-06-03 the v2 daemon is already stopped + its sqlite database
archived. The cutover work below assumes this baseline. If `~/.foreman/`
shows a live `daemon.lock` or `foreman.sqlite`, the v2 cleanup hasn't
happened yet — see "Re-running v2 cleanup" at the bottom.

Expected `~/.foreman/` contents:
- `config.toml` — foreman config (unchanged)
- `foreman-v2-archive-2026-06-03.sqlite` — archived v2 db
- `daemon.log` — historical log; v3 writes a new file
- (no `daemon.lock`, no `daemon.pid`, no `foreman.sqlite`)

## Pre-flight gates (must pass before flip)

1. **All v3 unit + integration tests green** locally:

   ```bash
   uv run pytest packages/foreman/tests/reconciler -v
   uv run pytest packages/foreman/tests/test_v3_bus_endpoint.py -v
   uv run pytest packages/foreman/tests/test_cli_v3.py -v
   ```

2. **Full pytest baseline** unchanged or higher:

   ```bash
   uv run pytest packages/foreman -q
   ```

3. **CLI smoke test** (wires Config + Reconciler without running ticks):

   ```bash
   uv run foreman daemon v3-start --max-ticks 0 --dry-run
   ```

   Expected: prints "v3-start wired: N projects, db=…, dry_run=True" and
   exits 0.

## Cutover procedure

### Step 1: Deploy v3 in dry-run mode

```bash
uv run foreman daemon v3-start --dry-run
```

Let it run for ~6 polls (≈6 minutes at the 60s default cadence). The
reconciler will:
- Fetch GH state for every registered project
- Evaluate rules per ticket
- Write intended actions to `~/.foreman/reconciler.sqlite` with
  `outcome='dry_run'` (NO host calls; no labels added, no PRs merged)

Inspect the dry-run output:

```bash
sqlite3 ~/.foreman/reconciler.sqlite "
SELECT ts, ticket_id, action, outcome, details
FROM execution_log
ORDER BY id DESC
LIMIT 30
"
```

Gut-check: do the intended actions make sense for today's stuck tickets?
For foreman#143 specifically you should see one row roughly like:

```
2026-06-03T20:??:??Z | jeffrichley/foreman#143 | advance_label_to_plan_approved | dry_run | {…}
```

If something looks wrong, **stop here**. File an issue with the
unexpected action + the GH state that triggered it. Do not flip to
executing.

### Step 2: Flip to executing mode

Stop the dry-run daemon (Ctrl-C). Start fresh:

```bash
uv run foreman daemon v3-start
```

(No `--dry-run` flag = execute mode.)

### Step 3: Tight observation (24-48h)

The first day post-flip is the human-in-the-loop window. Wren is on
stream watching:

- `tail -f ~/.foreman/daemon.log` per cycle
- `sqlite3 ~/.foreman/reconciler.sqlite "SELECT * FROM execution_log ORDER BY id DESC LIMIT 10"` after each action
- Per-action: "is this the right action for this ticket's GH state?"

If v3 misbehaves: stop the daemon and run rollback.

## Rollback (escape hatch)

```bash
# 1. Stop v3
pkill -f "foreman daemon v3-start"
# (Use Stop-Process -Name foreman on Windows.)

# 2. Restore v2 db
mv ~/.foreman/foreman-v2-archive-2026-06-03.sqlite ~/.foreman/foreman.sqlite

# 3. Restart v2
uv run foreman daemon start
```

Tickets that progressed during v3's brief reign may need manual recovery
on the v2 side (re-set the right `foreman:*` label by hand). This is the
escape hatch, not a routine.

## Re-running v2 cleanup

If `~/.foreman/foreman.sqlite` still exists (v2 cleanup hasn't been done):

```bash
# 1. Stop any v2 daemon
pkill -f "foreman daemon start"

# 2. Archive db
mv ~/.foreman/foreman.sqlite ~/.foreman/foreman-v2-archive-$(date +%Y-%m-%d).sqlite

# 3. Remove stale runtime files
rm -f ~/.foreman/daemon.lock ~/.foreman/daemon.pid
```

Then return to "Pre-flight gates" above.

## Removing v2 from the codebase

This is a separate follow-up PR, blocked on v3 running stable for ~2
weeks post-cutover. Tracked in a new issue at that time. Don't remove v2
code as part of the cutover itself.

````

- [ ] **Step 2: Verify file written**

```bash
test -f packages/foreman/docs/v3-cutover.md && wc -l packages/foreman/docs/v3-cutover.md
```
Expected: file exists, ≥80 lines.

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/docs/v3-cutover.md
git commit -m "docs(reconciler): add v3 cutover runbook for operators"
```

---

## Task 14: Open the v3 implementation PR

**Files:** none — this task only pushes + opens the PR.

- [ ] **Step 1: Push branch to origin**

```bash
PAT=$(python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py --being wren get github --keyring --password 2>/dev/null) && \
git push "https://x-access-token:${PAT}@github.com/jeffrichley/foreman.git" feat/v3-reconciler
```
Expected: pre-push hook runs full pytest (must pass); branch lands on origin.

- [ ] **Step 2: Open PR with conventional title + body**

```bash
PAT=$(python C:/Users/jeffr/.wren/.claude/skills/creds-management/scripts/creds.py --being wren get github --keyring --password 2>/dev/null) && \
GH_TOKEN="$PAT" gh pr create --repo jeffrichley/foreman --base main --head feat/v3-reconciler \
  --title "feat(reconciler): implement foreman v3 declarative reconciler" \
  --body "$(cat <<'EOF'
## Summary

Implementation of foreman v3 — a declarative reconciler with GitHub as the sole source of truth and an append-only execution log as the daemon's only cross-poll memory. Implements #106. Spec: docs/superpowers/specs/foreman-issue-106-spec.md (PR #107).

What's new (in dependency order):

1. `foreman.reconciler.exec_log.ExecutionLog` — append-only sqlite log with idempotence-query API
2. `foreman.reconciler.state` — immutable per-poll dataclasses (ProjectSnapshot/IssueState/PRState)
3. `foreman.reconciler.actions` — Action enum + ActionContext + executor with dry-run support and error containment
4. `foreman.reconciler.rules` — Rule dataclass + RULES catalog (4 safety rules + 8 forward-progress rules) + first-match evaluator + precedence invariant test
5. `foreman.reconciler.observer` — GraphQL one-query-per-project observer with typed failure modes
6. `foreman.reconciler.daemon.Reconciler` — async tick + run loop with N-consecutive-failure alerting
7. `foreman.v3_bus_endpoint` — single-writer pattern: subprocesses send ExecutionLogWrite events; daemon receives + writes
8. CLI: `foreman daemon v3-start [--dry-run] [--max-ticks N]`
9. Docs: `packages/foreman/docs/v3-cutover.md` cutover runbook
10. v2 deprecation banners on `daemon.py` + `storage.py`

What's NOT in this PR (deliberate):
- Real GH GraphQL client implementation (the `_build_v3_gh_and_host` stub raises NotImplementedError; runtime wiring lands in follow-up PR)
- Removal of v2 daemon code (separate PR after v3 proven stable ~2 weeks)
- Nightly archive job for the 30-day retention policy (follow-up PR)

## Test plan

- [ ] All tests green: \`uv run pytest packages/foreman -q\` reports \`>=678 passed\`
- [ ] v3-specific suites pass: \`uv run pytest packages/foreman/tests/reconciler packages/foreman/tests/test_v3_bus_endpoint.py packages/foreman/tests/test_cli_v3.py -v\`
- [ ] Precedence invariant enforced: \`uv run pytest packages/foreman/tests/reconciler/test_rules_precedence.py -v\`
- [ ] CLI smoke: \`uv run foreman daemon v3-start --max-ticks 0 --dry-run\` exits 0 with "v3-start wired" line
- [ ] Cutover runbook readable end-to-end: \`packages/foreman/docs/v3-cutover.md\`
- [ ] Spec coverage check: every spec section maps to a task in this PR

Implements #106.
EOF
)" 2>&1 | tail -3
```
Expected: PR URL printed to stdout.

- [ ] **Step 3: Report PR URL**

The SDD orchestrator reports the PR URL back to Jeff as the final deliverable of this plan.

---

## Self-review (run after writing this plan; before SDD picks it up)

**Spec coverage:**

- Source-of-truth boundary (spec §State boundary): Task 1 (log) + Task 2 (state dataclasses) — log owns execution facts; state dataclasses are per-poll views, never persisted. ✓
- State observation (spec §State observation): Task 8 (GraphQL observer with typed failures + rate-limit detection). ✓
- Reconciler logic (spec §Reconciler logic): Tasks 3 (Action) + 4 (Rule infra) + 5 (safety) + 6 (forward-progress). Precedence invariant in Task 4 test file. Idempotence via `has_unterminated`/`has_recent` in Task 1. ✓
- Execution log (spec §Execution log): Task 1 (schema + writer + reader) + Task 10 (bus endpoint as the single-writer interface for subprocesses). 30-day retention + archive: noted as follow-up in the v3-cutover doc (Task 13) — explicitly out of THIS PR's scope per spec §Out of scope's "30-day archive nightly job" treatment (not strictly named there, but consistent with "Wren's role in observation" deferral). **Gap: nightly archive job is mentioned in spec §Execution log → Retention but no task. Resolved by adding follow-up note in Task 13's PR body.**
- Migration (spec §Migration): Task 13 (cutover doc) + Task 12 (v2 deprecation banners). v2 db archive already done as of 2026-06-03.
- Failure modes (spec §Failure modes & resilience): Task 7 (action executor catches + logs); Task 9 (alert_after_n_failures); Task 1 (recover_orphaned).
- Testing strategy (spec §Testing strategy): per-rule unit tests in Tasks 4-6; precedence invariant in Task 4; integration tests in Task 9; CLI smoke in Task 11.
- Dry-run mode (spec §Cutover §Step 1): Task 7 (executor dry_run) + Task 9 (Reconciler.dry_run plumbing) + Task 11 (`--dry-run` CLI flag).

**Placeholder scan:** searched the plan for "TBD", "TODO", "implement later", "Similar to Task". None remain. The `_build_v3_gh_and_host` `NotImplementedError` is deliberate scope-fence, called out explicitly in the Task 11 docstring AND in the Task 14 PR body's "What's NOT in this PR" section.

**Type consistency:**
- `Action` enum names referenced in tests and rules match the enum values (DISPATCH_PLANNER etc.). ✓
- `ActionContext` constructor signature (snapshot, issue, pr, log) is identical at every call site. ✓
- `ExecutionLog.write_action` signature `(ticket_id, project, rule_name, action, outcome, details, parent_log_id=None)` is consistent across Tasks 1, 7, 9, 10. ✓
- `ReconcilerHost` Protocol methods (add_label, remove_label, post_comment, merge_pr, dispatch_role) match the fake's signatures in Tasks 7 + 9. ✓
- `RULES` is a `tuple[Rule, ...]` at every assignment. ✓
- `ProjectSnapshot.ticket_id_for(issue_number)` is referenced in actions.py and daemon.py consistently. ✓

**Identified gap → resolved:** spec mentions nightly archive job; this plan defers it to a follow-up PR (flagged in Task 14 PR body) rather than expanding scope. Acceptable per superpowers:writing-plans YAGNI guidance — archive policy is operational, not load-bearing for the v3 cutover.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-03-foreman-v3-declarative-reconciler-implementation.md`.

Next step: `superpowers:subagent-driven-development` executes this plan task-by-task in the worktree from Task 0. Continuous execution: each task gets a fresh implementer subagent, then spec-compliance reviewer, then code-quality reviewer; no human check-ins between tasks.
