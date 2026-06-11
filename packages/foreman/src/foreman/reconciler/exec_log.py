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

# foreman#228: outcomes that do NOT count as same-role failures for the
# consecutive-failure rate-limit predicate. ``success`` clears the counter
# (the contract says "Same-role success only resets the counter"). The
# administrative outcomes — ``dry_run``, ``skipped_capacity`` —
# represent no real dispatch attempt and never burn budget. ``running``
# is the start-row marker, not a termination; it is filtered out by the
# ``parent_log_id IS NOT NULL`` clause in the counter query.
_NON_FAILURE_OUTCOMES: tuple[str, ...] = (
    "success",
    "dry_run",
    "skipped_capacity",
)

# foreman#228: the synthetic ``action`` value the rate-limit reset
# sentinel uses. Stored as ``rate_limit_reset:<dispatch_action>`` so the
# (action, ticket_id) shape mirrors every other query in this module and
# the per-action scope falls out naturally. ``outcome='reset'`` keeps
# the row out of every existing terminator-counting query.
_RATE_LIMIT_RESET_ACTION_PREFIX = "rate_limit_reset:"
_RATE_LIMIT_RESET_OUTCOME = "reset"

# foreman#251 (Phase 1 of dispatch-recorder design): the execution_log
# becomes the canonical cost ledger. ``CURRENT_SCHEMA_VERSION`` bumps
# whenever the table layout changes; ``init()`` reads the live
# ``PRAGMA user_version`` and runs forward-only migrations until it
# matches. Version 0 → 1 adds the eight cost columns.
CURRENT_SCHEMA_VERSION = 1

# foreman#251: the eight nullable cost columns added at schema version 1.
# Single source of truth so the migration and the writer share the same
# spelling — a typo at one site would silently produce a NULL column at
# the other and break "cost in execution_log == cost in JSONL" parity.
_COST_COLUMNS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"),
    ("cache_creation_input_tokens", "INTEGER"),
    ("cache_read_input_tokens", "INTEGER"),
    ("total_cost_usd", "REAL"),
    ("model_usage_json", "TEXT"),
    ("duration_ms", "INTEGER"),
    ("num_turns", "INTEGER"),
)

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


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the live column-name set for ``table``.

    Used by the migration runner to make ``ADD COLUMN`` idempotent —
    re-running ``init()`` against an already-migrated DB must be a
    no-op, not a duplicate-column error.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk.
    return {row[1] for row in rows}


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply forward-only schema migrations until ``user_version`` matches
    :data:`CURRENT_SCHEMA_VERSION`.

    Each migration step is idempotent on a partially-migrated DB — re-running
    ``init()`` against a DB that already carries the cost columns (e.g.,
    a daemon that crashed mid-migration and restarted) must not raise on
    duplicate-column. We re-read ``PRAGMA table_info`` after each step
    rather than trust the bookkeeping, because the bookkeeping (``PRAGMA
    user_version``) is a single integer with no transaction guarantee
    across the ``ALTER TABLE`` statements that change the actual schema.
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])

    if current < 1:
        # Phase 1 of foreman#251: add the eight cost columns. All nullable
        # so existing rows (start-of-action rows, non-role-dispatch rows)
        # stay valid; only ``record_dispatch_complete`` writes ever
        # populate them. The migration uses ``ADD COLUMN`` rather than a
        # table rebuild — sqlite has supported ``ADD COLUMN`` since 3.2.0
        # and it's a metadata-only change with no row rewrite.
        existing = _existing_columns(conn, "execution_log")
        for col_name, col_type in _COST_COLUMNS:
            if col_name in existing:
                # Idempotency guard: if a previous init() partially
                # ran (added some columns then crashed before bumping
                # user_version), skip the ones already present.
                continue
            conn.execute(
                f"ALTER TABLE execution_log ADD COLUMN {col_name} {col_type}"
            )

    if current != CURRENT_SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")


class ExecutionLog:
    """Append-only sqlite log of reconciler decisions and their outcomes."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def init(self) -> None:
        """Create schema + indexes, run migrations. Idempotent.

        foreman#251 (Phase 1 of the dispatch-recorder design): after the
        base schema lands, ``_migrate`` advances the live DB to
        :data:`CURRENT_SCHEMA_VERSION` by running forward-only steps.
        Each step is idempotent on a partially-migrated DB so a daemon
        that crashed mid-migration and restarted ends up in the same
        state as one that didn't.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            for stmt in _SCHEMA:
                conn.execute(stmt)
            _migrate(conn)

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
        usage_columns: dict[str, Any] | None = None,
    ) -> int:
        """Insert a row. Returns the new row id.

        foreman#251 (Phase 1): ``usage_columns`` carries the eight cost
        fields when the caller is a :class:`DispatchRecorder` cost
        subscriber. Keys must be a subset of the schema's cost column
        names (:data:`_COST_COLUMNS`); unknown keys are rejected to
        catch typos at the call site rather than as silent NULLs.
        ``None`` keeps the existing eight-positional INSERT — old
        callers don't pay any new cost.
        """
        details_json = json.dumps(details, sort_keys=True)
        with self._connect() as conn:
            if usage_columns:
                self._validate_usage_columns(usage_columns)
                col_names = list(usage_columns.keys())
                extra_columns = ", " + ", ".join(col_names)
                extra_placeholders = ", " + ", ".join("?" for _ in col_names)
                extra_values: tuple[Any, ...] = tuple(usage_columns[k] for k in col_names)
            else:
                extra_columns = ""
                extra_placeholders = ""
                extra_values = ()
            cur = conn.execute(
                f"""
                INSERT INTO execution_log
                    (ticket_id, project, rule_name, action, outcome, details, parent_log_id{extra_columns})
                VALUES (?, ?, ?, ?, ?, ?, ?{extra_placeholders})
                """,
                (
                    ticket_id,
                    project,
                    rule_name,
                    action,
                    outcome,
                    details_json,
                    parent_log_id,
                    *extra_values,
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def terminate_action(
        self,
        *,
        parent_log_id: int,
        outcome: str,
        details: dict[str, Any],
        usage_columns: dict[str, Any] | None = None,
    ) -> int:
        """Write a termination row pointing at the start row.

        Inherits ticket_id / project / action / rule_name from the parent so
        the pair is queryable as one unit. Outcome reflects success | error.

        foreman#251 (Phase 1): ``usage_columns`` accepts the same eight
        cost fields :meth:`write_action` does, for the (b) variant in
        the design (terminate row carries cost rather than a sibling
        ``<role>_complete`` row). The existing ``terminate_dispatch``
        flow keeps passing ``None`` and is untouched.
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
            if usage_columns:
                self._validate_usage_columns(usage_columns)
                col_names = list(usage_columns.keys())
                extra_columns = ", " + ", ".join(col_names)
                extra_placeholders = ", " + ", ".join("?" for _ in col_names)
                extra_values: tuple[Any, ...] = tuple(usage_columns[k] for k in col_names)
            else:
                extra_columns = ""
                extra_placeholders = ""
                extra_values = ()
            cur = conn.execute(
                f"""
                INSERT INTO execution_log
                    (ticket_id, project, rule_name, action, outcome, details, parent_log_id{extra_columns})
                VALUES (?, ?, ?, ?, ?, ?, ?{extra_placeholders})
                """,
                (
                    ticket_id,
                    project,
                    rule_name,
                    action,
                    outcome,
                    details_json,
                    parent_log_id,
                    *extra_values,
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    @staticmethod
    def _validate_usage_columns(usage_columns: dict[str, Any]) -> None:
        """Raise if ``usage_columns`` has keys not in the cost-column set.

        foreman#251: silent-typo guard. A caller that misspells
        ``input_tokens`` as ``input_token`` would otherwise write the
        scalar into a nowhere column and ``write_action`` would
        silently insert NULLs. Raising loudly here surfaces the bug at
        the call site rather than at a later query.
        """
        allowed = {name for name, _type in _COST_COLUMNS}
        unknown = set(usage_columns.keys()) - allowed
        if unknown:
            raise ValueError(
                f"unknown cost columns in usage_columns: {sorted(unknown)!r}; "
                f"allowed: {sorted(allowed)!r}"
            )

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

    def count_recent_failures(
        self,
        *,
        action: str,
        ticket_id: str,
        within_seconds: int,
    ) -> int:
        """Count consecutive failure terminations for the per-ticket-per-role
        rate-limit (foreman#228).

        A "failure" is a terminator row (``parent_log_id IS NOT NULL``)
        for the named ``action``/``ticket_id`` whose ``outcome`` is NOT
        in :data:`_NON_FAILURE_OUTCOMES` (success / dry_run /
        skipped_capacity). The count is filtered to the last
        ``within_seconds`` AND to rows whose ``ts`` is greater than the
        most recent reset signal — where a "reset signal" is EITHER:

        * a same-(action, ticket_id) ``outcome='success'`` termination
          (per the contract: "Same-role success only resets the
          counter"), OR
        * a ``rate_limit_reset:<action>`` sentinel row written by
          :meth:`write_rate_limit_reset` (per the contract's
          reset-on-needs-help-removal path — the trip writes one to
          ensure the human's re-queue gives a fresh window
          deterministic regardless of timing).

        The query uses string comparison against SQLite's
        ``CURRENT_TIMESTAMP`` format (``YYYY-MM-DD HH:MM:SS``) so it
        matches the existing ``has_recent`` convention.

        **Post-daemon-restart semantics.** ``recover_orphaned`` writes
        ``outcome='errored:recovery'`` rows for any ``running`` start
        rows orphaned by a daemon crash. Those rows ARE terminator rows
        and ``errored:recovery`` is NOT in :data:`_NON_FAILURE_OUTCOMES`,
        so they count as failures here. Intentional: a crashed daemon
        that was mid-dispatch represents a real (incomplete) attempt,
        and over-counting is safer than under-counting for runaway
        protection. The practical consequence is that a daemon crash
        during a busy ticket can pre-load the failure counter and
        cause an early rate-limit trip on the first poll after restart
        — operators can re-queue normally (the trip writes the reset
        sentinel, so needs-help removal clears the counter).
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=within_seconds)
        cutoff_sql = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        reset_action = f"{_RATE_LIMIT_RESET_ACTION_PREFIX}{action}"
        with self._connect() as conn:
            # Compute the reset fence: latest of {last same-role success
            # termination, last rate_limit_reset sentinel}.
            success_row = conn.execute(
                """
                SELECT MAX(ts) FROM execution_log
                WHERE action = ?
                  AND ticket_id = ?
                  AND parent_log_id IS NOT NULL
                  AND outcome = 'success'
                """,
                (action, ticket_id),
            ).fetchone()
            reset_row = conn.execute(
                """
                SELECT MAX(ts) FROM execution_log
                WHERE action = ?
                  AND ticket_id = ?
                  AND outcome = ?
                """,
                (reset_action, ticket_id, _RATE_LIMIT_RESET_OUTCOME),
            ).fetchone()
            last_success_ts: str | None = success_row[0] if success_row else None
            last_reset_ts: str | None = reset_row[0] if reset_row else None
            # The fence is the later of the two timestamps (string
            # comparison works on the ISO-shaped values SQLite stores).
            fence_ts: str | None
            if last_success_ts is None and last_reset_ts is None:
                fence_ts = None
            elif last_success_ts is None:
                fence_ts = last_reset_ts
            elif last_reset_ts is None:
                fence_ts = last_success_ts
            else:
                fence_ts = max(last_success_ts, last_reset_ts)
            placeholders = ",".join("?" for _ in _NON_FAILURE_OUTCOMES)
            params: list[Any] = [action, ticket_id, *_NON_FAILURE_OUTCOMES, cutoff_sql]
            sql = f"""
                SELECT COUNT(*) FROM execution_log
                WHERE action = ?
                  AND ticket_id = ?
                  AND parent_log_id IS NOT NULL
                  AND outcome NOT IN ({placeholders})
                  AND ts > ?
            """
            if fence_ts is not None:
                sql += " AND ts > ?"
                params.append(fence_ts)
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row else 0

    def write_rate_limit_reset(
        self,
        *,
        action: str,
        ticket_id: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        """Write a rate-limit reset sentinel for (``action``, ``ticket_id``).

        Used by the foreman#228 rate-limit trip handler to clear the
        counter so the human's subsequent re-queue (remove
        ``foreman:needs-help`` + re-add the entry-point label) gives the
        role a fresh window — even if the W window hasn't expired.

        The row carries:

        * ``action = "rate_limit_reset:<action>"`` so the synthetic
          marker lives in its own action namespace (existing queries
          that filter on ``action = "dispatch_*"`` are unaffected).
        * ``outcome = "reset"`` so the row is never counted by
          ``count_completed`` (which filters on terminations with
          ``parent_log_id IS NOT NULL``).
        * No ``parent_log_id`` (the reset is not a termination of any
          prior row).
        """
        return self.write_action(
            ticket_id=ticket_id,
            project="foreman",
            rule_name=None,
            action=f"{_RATE_LIMIT_RESET_ACTION_PREFIX}{action}",
            outcome=_RATE_LIMIT_RESET_OUTCOME,
            details=details or {},
        )

    def recent_failure_details(
        self,
        *,
        action: str,
        ticket_id: str,
        within_seconds: int,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` recent failure rows' ``details`` for the
        rate-limit trip comment, newest first.

        Same window + fence semantics as :meth:`count_recent_failures`
        so the comment surfaces exactly the failures that caused the
        trip.
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=within_seconds)
        cutoff_sql = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        reset_action = f"{_RATE_LIMIT_RESET_ACTION_PREFIX}{action}"
        with self._connect() as conn:
            success_row = conn.execute(
                """
                SELECT MAX(ts) FROM execution_log
                WHERE action = ? AND ticket_id = ?
                  AND parent_log_id IS NOT NULL AND outcome = 'success'
                """,
                (action, ticket_id),
            ).fetchone()
            reset_row = conn.execute(
                """
                SELECT MAX(ts) FROM execution_log
                WHERE action = ? AND ticket_id = ? AND outcome = ?
                """,
                (reset_action, ticket_id, _RATE_LIMIT_RESET_OUTCOME),
            ).fetchone()
            last_success_ts: str | None = success_row[0] if success_row else None
            last_reset_ts: str | None = reset_row[0] if reset_row else None
            fence_ts: str | None
            if last_success_ts is None and last_reset_ts is None:
                fence_ts = None
            elif last_success_ts is None:
                fence_ts = last_reset_ts
            elif last_reset_ts is None:
                fence_ts = last_success_ts
            else:
                fence_ts = max(last_success_ts, last_reset_ts)
            placeholders = ",".join("?" for _ in _NON_FAILURE_OUTCOMES)
            params: list[Any] = [action, ticket_id, *_NON_FAILURE_OUTCOMES, cutoff_sql]
            sql = f"""
                SELECT ts, outcome, details FROM execution_log
                WHERE action = ?
                  AND ticket_id = ?
                  AND parent_log_id IS NOT NULL
                  AND outcome NOT IN ({placeholders})
                  AND ts > ?
            """
            if fence_ts is not None:
                sql += " AND ts > ?"
                params.append(fence_ts)
            sql += " ORDER BY ts DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for ts, outcome, details_json in rows:
            try:
                parsed = json.loads(details_json) if details_json else {}
            except json.JSONDecodeError:
                parsed = {"raw_details": details_json}
            out.append({"ts": ts, "outcome": outcome, "details": parsed})
        return out

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
