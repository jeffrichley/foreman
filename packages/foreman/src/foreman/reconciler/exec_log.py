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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Outcome sentinels hoisted so the partial index DDL, query predicates, and
# recovery writer share one source of truth. A typo or rename at one site
# would silently degrade the index without breaking tests.
_OUTCOME_RUNNING = "running"
_OUTCOME_ERRORED_RECOVERY = "errored:recovery"

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
    # F-string substitution into static DDL is safe — no user input touches
    # it. SQLite partial-index predicates cannot be parameterized.
    f"""
    CREATE INDEX IF NOT EXISTS idx_running ON execution_log(outcome)
    WHERE outcome = '{_OUTCOME_RUNNING}'
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
            assert cur.lastrowid is not None
            return cur.lastrowid

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
            assert cur.lastrowid is not None
            return cur.lastrowid

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
                  AND start.outcome = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM execution_log term
                      WHERE term.parent_log_id = start.id
                  )
                LIMIT 1
                """,
                (ticket_id, action, _OUTCOME_RUNNING),
            ).fetchone()
            return row is not None

    def has_recent(self, action: str, ticket_id: str, *, within_seconds: int) -> bool:
        """True iff there's any row for (action, ticket_id) with ts within the
        last `within_seconds` seconds. Used for surface_help alert rate-limit.
        """
        # SQLite's CURRENT_TIMESTAMP writes UTC as 'YYYY-MM-DD HH:MM:SS' (no
        # 'T' separator, no tz suffix). Match that format so string comparison
        # works correctly against the stored ts column.
        cutoff = datetime.now(UTC) - timedelta(seconds=within_seconds)
        cutoff_sql = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM execution_log
                WHERE ticket_id = ? AND action = ? AND ts > ?
                LIMIT 1
                """,
                (ticket_id, action, cutoff_sql),
            ).fetchone()
            return row is not None

    def count_completed(
        self, action: str, ticket_id: str, *, outcome: str | None = None
    ) -> int:
        """Count terminated attempts for (action, ticket_id).

        A "completed" attempt is a row whose `parent_log_id` is NOT NULL —
        i.e., a termination row pointing back at a start row. Unterminated
        running rows don't count.

        When ``outcome`` is None (default), counts terminations that
        represent real work — successes, errors, timeouts, recovery
        failures — and EXCLUDES ``skipped_capacity``. The cap-skip
        happens before any role subprocess runs, so it isn't a real
        attempt against the budget. This matches the explicit invariant
        in ``actions.py``'s dispatch-role terminator: "a
        ``skipped_capacity`` row does NOT burn budget — the cap-skip
        is rule-neutral, which is what we want." Closes foreman#174 —
        previously a queue-waiter ticket would get escalated to
        ``foreman:needs-help`` after 3 cap-skips even though the
        Worker never ran.

        When ``outcome`` is a string, filters to rows with that exact
        outcome — ``skipped_capacity`` IS included if explicitly
        requested, so callers that genuinely want to count cap-skips
        (e.g., observability, a future stuck-pipeline detector) can
        pass ``outcome="skipped_capacity"`` and get the raw count
        back. Callers that want success-only counts pass
        ``outcome="success"``.
        """
        with self._connect() as conn:
            if outcome is None:
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM execution_log
                    WHERE action = ?
                      AND ticket_id = ?
                      AND parent_log_id IS NOT NULL
                      AND outcome != 'skipped_capacity'
                    """,
                    (action, ticket_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM execution_log
                    WHERE action = ?
                      AND ticket_id = ?
                      AND parent_log_id IS NOT NULL
                      AND outcome = ?
                    """,
                    (action, ticket_id, outcome),
                ).fetchone()
            return int(row[0]) if row else 0

    def recover_orphaned(self) -> int:
        """On daemon restart: any outcome='running' row with no termination
        means the daemon crashed mid-action. Mark each as terminated with
        outcome='errored:recovery'. Returns the count of rows recovered.
        """
        with self._connect() as conn:
            orphans = conn.execute(
                """
                SELECT start.id FROM execution_log start
                WHERE start.outcome = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM execution_log term
                      WHERE term.parent_log_id = start.id
                  )
                """,
                (_OUTCOME_RUNNING,),
            ).fetchall()
        count = 0
        for (parent_id,) in orphans:
            self.terminate_action(
                parent_log_id=parent_id,
                outcome=_OUTCOME_ERRORED_RECOVERY,
                details={"reason": "daemon restart found orphaned running row"},
            )
            count += 1
        return count
