"""Tests for the v3 execution log — schema, writer, idempotence reader."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from foreman.reconciler.exec_log import (
    _COST_COLUMNS,
    CURRENT_SCHEMA_VERSION,
    ExecutionLog,
)


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


def test_has_recent_returns_false_outside_window(tmp_path: Path) -> None:
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
    # Window of 0 seconds means even a just-inserted row is outside it
    # (strict ts > cutoff requires the row to be older than the cutoff).
    assert log.has_recent("surface_help", "jeffrichley/foreman#143", within_seconds=0) is False


def test_terminate_action_raises_on_unknown_parent(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    with pytest.raises(ValueError, match="No log row with id="):
        log.terminate_action(parent_log_id=9999, outcome="success", details={})


def test_count_completed_counts_terminated_attempts(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    # 2 completed dispatch_fixer attempts for ticket A; 1 unterminated; 1 for ticket B
    for _ in range(2):
        start_id = log.write_action(
            ticket_id="jeffrichley/foreman#1",
            project="foreman",
            rule_name="dispatch_fixer",
            action="dispatch_fixer",
            outcome="running",
            details={},
        )
        log.terminate_action(parent_log_id=start_id, outcome="success", details={})
    # Add 1 unterminated row — should NOT count
    log.write_action(
        ticket_id="jeffrichley/foreman#1",
        project="foreman",
        rule_name="dispatch_fixer",
        action="dispatch_fixer",
        outcome="running",
        details={},
    )
    # Different ticket — should NOT count toward #1's total
    other_start = log.write_action(
        ticket_id="jeffrichley/foreman#2",
        project="foreman",
        rule_name="dispatch_fixer",
        action="dispatch_fixer",
        outcome="running",
        details={},
    )
    log.terminate_action(parent_log_id=other_start, outcome="error", details={})

    assert log.count_completed("dispatch_fixer", "jeffrichley/foreman#1") == 2
    assert log.count_completed("dispatch_fixer", "jeffrichley/foreman#2") == 1
    assert log.count_completed("dispatch_fixer", "jeffrichley/foreman#999") == 0


def test_count_completed_filters_by_outcome(tmp_path: Path) -> None:
    """``count_completed`` supports filtering terminated rows by outcome.

    Idempotence gates (one Planner per ticket, one spec-reviewer per spec PR)
    need success-only counts so a crashed/recovered run doesn't permanently
    block legitimate re-fire. Budget gates (max-N attempts) want the default
    all-terminations count so failures DO burn a budget slot.
    """
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    # Seed: one success termination, one error termination, both for the
    # same (action, ticket) pair.
    start_id = log.write_action(
        ticket_id="jeffrichley/foreman#1",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )
    log.terminate_action(parent_log_id=start_id, outcome="success", details={})

    start_id = log.write_action(
        ticket_id="jeffrichley/foreman#1",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )
    log.terminate_action(parent_log_id=start_id, outcome="error", details={})

    # Default (outcome=None): counts all terminations — 2 (1 success + 1 error).
    assert log.count_completed("dispatch_planner", "jeffrichley/foreman#1") == 2
    # outcome="success": counts only the successful termination — 1.
    assert (
        log.count_completed(
            "dispatch_planner", "jeffrichley/foreman#1", outcome="success"
        )
        == 1
    )
    # outcome="error": counts only the error termination — 1.
    assert (
        log.count_completed(
            "dispatch_planner", "jeffrichley/foreman#1", outcome="error"
        )
        == 1
    )
    # outcome="timeout": no rows match — 0.
    assert (
        log.count_completed(
            "dispatch_planner", "jeffrichley/foreman#1", outcome="timeout"
        )
        == 0
    )


def test_count_completed_excludes_skipped_capacity_by_default(tmp_path: Path) -> None:
    """foreman#174 regression guard.

    Default ``count_completed`` (no ``outcome`` filter) is the path the
    budget-gate rules use (``_impl_attempts_exhausted``,
    ``_fix_attempts_exhausted``, etc.). It must EXCLUDE
    ``skipped_capacity`` terminations — a cap-skip happens before any
    role subprocess runs, so it isn't a real attempt against the
    budget. Previously a queue-waiter ticket got escalated to
    ``foreman:needs-help`` after 3 cap-skips even though the Worker
    never ran (live trace on issue #170, 2026-06-07 00:21–00:24).
    """
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    ticket_id = "owner/repo#1"

    # Three cap-skipped dispatch_worker attempts.
    for _ in range(3):
        start_id = log.write_action(
            ticket_id=ticket_id,
            project="owner",
            rule_name="dispatch_worker",
            action="dispatch_worker",
            outcome="running",
            details={},
        )
        log.terminate_action(
            parent_log_id=start_id, outcome="skipped_capacity", details={}
        )

    # Default path: cap-skips don't burn budget. The 3 termination rows
    # exist, but the budget-gate-relevant count is 0.
    assert log.count_completed("dispatch_worker", ticket_id) == 0

    # Explicit ``outcome="skipped_capacity"`` still returns the raw count
    # for callers that genuinely want to track cap-skips (observability,
    # a future stuck-pipeline detector).
    assert (
        log.count_completed(
            "dispatch_worker", ticket_id, outcome="skipped_capacity"
        )
        == 3
    )


def test_init_adds_eight_cost_columns(tmp_path: Path) -> None:
    """foreman#251 (Phase 1): schema bumps to version 1 and adds eight
    nullable cost columns to ``execution_log``. The columns are
    NULL-by-default so existing rows (start rows, lifecycle markers)
    stay valid; only ``DispatchRecorder`` writes ever populate them."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(execution_log)").fetchall()}
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]

    expected_cost_columns = {name for name, _type in _COST_COLUMNS}
    assert expected_cost_columns <= columns
    assert int(user_version) == CURRENT_SCHEMA_VERSION


def test_migration_is_idempotent_across_repeated_init(tmp_path: Path) -> None:
    """foreman#251 (Phase 1): re-running ``init()`` against an
    already-migrated DB must NOT raise on duplicate-column. This
    protects the daemon-restart path where a crashed daemon left the
    DB in an unknown state."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    log.init()  # second call: must be a no-op.
    log.init()  # third call: still a no-op.

    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        # No duplicate columns (PRAGMA returns distinct rows even if
        # the table had been re-altered; the guard is the no-raise).
        columns = [row[1] for row in conn.execute("PRAGMA table_info(execution_log)").fetchall()]
    assert len(columns) == len(set(columns))


def test_migration_recovers_from_partial_alter_table_state(tmp_path: Path) -> None:
    """foreman#251 (Phase 1): if a previous init() crashed after
    adding SOME of the cost columns but before bumping user_version,
    the next init() finishes the migration without raising."""
    db_path = tmp_path / "log.sqlite"
    # Manually create a v0 schema with some cost columns already there.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE execution_log (
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
            """
        )
        # Pretend a partial migration added only the first two cost columns.
        conn.execute("ALTER TABLE execution_log ADD COLUMN input_tokens INTEGER")
        conn.execute("ALTER TABLE execution_log ADD COLUMN output_tokens INTEGER")
        # user_version stays 0 so the migration runs from the top.

    # Now init() must complete the migration without raising
    # "duplicate column name" for input_tokens / output_tokens.
    log = ExecutionLog(db_path)
    log.init()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(execution_log)").fetchall()}
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    expected_cost_columns = {name for name, _type in _COST_COLUMNS}
    assert expected_cost_columns <= columns
    assert int(user_version) == CURRENT_SCHEMA_VERSION


def test_write_action_persists_cost_columns_when_supplied(tmp_path: Path) -> None:
    """foreman#251 (Phase 1): ``write_action`` accepts an optional
    ``usage_columns`` dict whose keys are the cost-column names; the
    INSERT carries them through. Old call sites that pass nothing get
    the existing seven-column INSERT unchanged."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    row_id = log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_worker",
        action="dispatch_worker",
        outcome="running",
        details={},
        usage_columns={
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_cost_usd": 0.42,
        },
    )

    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        row = conn.execute(
            "SELECT input_tokens, output_tokens, total_cost_usd, num_turns "
            "FROM execution_log WHERE id = ?",
            (row_id,),
        ).fetchone()
    assert row[0] == 1000
    assert row[1] == 500
    assert row[2] == pytest.approx(0.42)
    # Unspecified column stays NULL.
    assert row[3] is None


def test_write_action_rejects_unknown_usage_column_keys(tmp_path: Path) -> None:
    """Silent-typo guard: a misspelled cost column key raises rather
    than silently inserting NULLs."""
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    with pytest.raises(ValueError, match="unknown cost columns"):
        log.write_action(
            ticket_id="jeffrichley/foreman#143",
            project="foreman",
            rule_name="dispatch_worker",
            action="dispatch_worker",
            outcome="running",
            details={},
            usage_columns={"input_token": 1000},  # typo: missing 's'
        )


def test_count_completed_default_still_counts_errors_and_successes(tmp_path: Path) -> None:
    """foreman#174 belt-and-suspenders: the fix only excludes
    ``skipped_capacity`` from the default-path budget count — successes,
    errors, timeouts, and recovery failures still count, because each
    represents a real attempt that burned a budget slot.
    """
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()

    ticket_id = "owner/repo#1"

    # success + error + timeout + skipped_capacity — only first three
    # should count against the budget.
    for outcome in ("success", "error", "timeout", "skipped_capacity"):
        start_id = log.write_action(
            ticket_id=ticket_id,
            project="owner",
            rule_name="dispatch_worker",
            action="dispatch_worker",
            outcome="running",
            details={},
        )
        log.terminate_action(
            parent_log_id=start_id, outcome=outcome, details={}
        )

    # 3 real attempts (skipped_capacity excluded).
    assert log.count_completed("dispatch_worker", ticket_id) == 3
