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
