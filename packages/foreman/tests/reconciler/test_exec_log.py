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
