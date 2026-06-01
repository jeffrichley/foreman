"""SQLite lifecycle storage for the Foreman daemon.

Stores pipelines, node runs, label transitions, failures, and last-seen
label snapshots used by the poller diff. Not load-bearing for correctness
(GitHub labels remain source of truth) — load-bearing for observability,
audit trail, and crash-recovery reconciliation.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
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
            assert cursor.lastrowid is not None  # INSERT always populates lastrowid
            return int(cursor.lastrowid)

    def get_pipeline(self, project: str, issue_number: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM pipelines WHERE project = ? AND issue_number = ?",
                (project, issue_number),
            ).fetchone()

    def mark_pipeline_terminated(self, project: str, issue_number: int, at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE pipelines SET terminated_at = ? WHERE project = ? AND issue_number = ?",
                (at.isoformat(), project, issue_number),
            )

    def iter_pipelines_in_flight(self) -> Iterator[sqlite3.Row]:
        with self.connect() as conn:
            yield from conn.execute("SELECT * FROM pipelines WHERE terminated_at IS NULL")

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
                "SELECT labels_json FROM labels_seen WHERE project = ? AND issue_number = ?",
                (project, issue_number),
            ).fetchone()
        if row is None:
            return None
        return list(json.loads(row["labels_json"]))

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
            assert cursor.lastrowid is not None  # INSERT always populates lastrowid
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
