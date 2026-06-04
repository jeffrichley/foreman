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
