"""Tests for the Phase 1 DispatchRecorder mediator (foreman#251).

The Recorder is the single writer of dispatch telemetry. Phase 1 is
additive — both ``execution_log`` (cost columns) and JSONL (via the
existing ``log_<role>_run`` writers) get dual-written. ``(trace_id,
event_kind)`` dedup makes double-write structurally impossible for
within-process emits; cross-process dedup (subprocess emits cost,
parent emits "subprocess terminated") goes through a DB lookup so the
parent can detect the subprocess already wrote a completion row.

The four mandatory test buckets from the spec:

1. Recorder dedup: second event for ``(trace_id, event_kind)`` is no-op.
2. Cost path: ``record_dispatch_complete`` writes the eight cost columns.
3. Subprocess-killed (no prior completion): writes ``subprocess_killed``
   to BOTH execution_log AND JSONL with zero cost.
4. Subprocess-killed (with prior completion): dedup catches it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from foreman.dispatch_recorder import (
    CostSubscriber,
    DispatchRecorder,
    RoleStatsSubscriber,
)
from foreman.provider import UsageInfo
from foreman.reconciler.exec_log import ExecutionLog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log(tmp_path: Path) -> ExecutionLog:
    log = ExecutionLog(tmp_path / "exec.sqlite")
    log.init()
    return log


@pytest.fixture
def recorder(tmp_path: Path, log: ExecutionLog) -> DispatchRecorder:
    return DispatchRecorder(log=log, stats_root=tmp_path / "stats")


def _dispatch_complete_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "trace_id": 1,
        "role": "planner",
        "repo_slug": "jeffrichley/voice",
        "ticket_id": "jeffrichley/voice#42",
        "project": "voice",
        "issue_number": 42,
        "pr_number": 99,
        "outcome": "spec_written",
        "usage": UsageInfo(
            input_tokens=1000,
            output_tokens=500,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=100,
            total_cost_usd=0.12,
            model_usage={"claude-sonnet": {"input_tokens": 1000}},
            duration_ms=12345,
            num_turns=4,
        ),
        "role_data": {},
        "duration_seconds": 87.5,
    }
    base.update(overrides)
    return base


def _seed_start_row(log: ExecutionLog, *, ticket_id: str, action: str) -> int:
    return log.write_action(
        ticket_id=ticket_id,
        project="voice",
        rule_name=f"{action}_rule",
        action=action,
        outcome="running",
        details={},
    )


# ---------------------------------------------------------------------------
# 1. Dedup — second event for same (trace_id, event_kind) is no-op.
# ---------------------------------------------------------------------------


def test_record_dispatch_complete_deduplicates_on_trace_id_and_event_kind(
    recorder: DispatchRecorder, log: ExecutionLog, tmp_path: Path
) -> None:
    """foreman#251 acceptance criterion: ``(trace_id, event_kind)`` dedup
    makes second-write a no-op. Pin both ledgers — neither gets a new
    row on the second emit."""
    trace_id = _seed_start_row(log, ticket_id="jeffrichley/voice#42", action="dispatch_planner")

    first_kwargs = _dispatch_complete_kwargs(trace_id=trace_id)
    recorder.record_dispatch_complete(**first_kwargs)  # type: ignore[arg-type]

    with sqlite3.connect(log.db_path) as conn:
        rows_after_first = conn.execute(
            "SELECT COUNT(*) FROM execution_log WHERE parent_log_id = ?",
            (trace_id,),
        ).fetchone()[0]

    jsonl_path = tmp_path / "stats" / "jeffrichley__voice" / "planner.jsonl"
    lines_after_first = jsonl_path.read_text(encoding="utf-8").splitlines()

    # Second emit with the same trace_id + event_kind: must be a no-op.
    recorder.record_dispatch_complete(**first_kwargs)  # type: ignore[arg-type]

    with sqlite3.connect(log.db_path) as conn:
        rows_after_second = conn.execute(
            "SELECT COUNT(*) FROM execution_log WHERE parent_log_id = ?",
            (trace_id,),
        ).fetchone()[0]
    lines_after_second = jsonl_path.read_text(encoding="utf-8").splitlines()

    assert rows_after_first == rows_after_second == 1
    assert len(lines_after_first) == len(lines_after_second) == 1


# ---------------------------------------------------------------------------
# 2. Cost path — record_dispatch_complete populates the eight cost columns.
# ---------------------------------------------------------------------------


def test_record_dispatch_complete_writes_cost_columns_into_execution_log(
    recorder: DispatchRecorder, log: ExecutionLog
) -> None:
    """The eight nullable cost columns get the provider-reported values."""
    trace_id = _seed_start_row(log, ticket_id="jeffrichley/voice#42", action="dispatch_planner")
    kwargs = _dispatch_complete_kwargs(trace_id=trace_id)
    recorder.record_dispatch_complete(**kwargs)  # type: ignore[arg-type]

    with sqlite3.connect(log.db_path) as conn:
        row = conn.execute(
            """
            SELECT input_tokens, output_tokens, cache_creation_input_tokens,
                   cache_read_input_tokens, total_cost_usd, model_usage_json,
                   duration_ms, num_turns, outcome
            FROM execution_log WHERE parent_log_id = ?
            """,
            (trace_id,),
        ).fetchone()

    assert row is not None
    (
        input_tokens,
        output_tokens,
        cache_creation_input_tokens,
        cache_read_input_tokens,
        total_cost_usd,
        model_usage_json,
        duration_ms,
        num_turns,
        outcome,
    ) = row
    assert input_tokens == 1000
    assert output_tokens == 500
    assert cache_creation_input_tokens == 200
    assert cache_read_input_tokens == 100
    assert total_cost_usd == pytest.approx(0.12)
    assert json.loads(model_usage_json) == {"claude-sonnet": {"input_tokens": 1000}}
    assert duration_ms == 12345
    assert num_turns == 4
    assert outcome == "spec_written"


# ---------------------------------------------------------------------------
# 3. Subprocess-killed without prior completion — writes to both ledgers.
# ---------------------------------------------------------------------------


def test_subprocess_terminated_without_prior_completion_writes_killed_row(
    recorder: DispatchRecorder, log: ExecutionLog, tmp_path: Path
) -> None:
    """foreman#251: ``record_subprocess_terminated`` with no prior
    ``record_dispatch_complete`` for the trace_id writes a
    ``subprocess_killed`` row to BOTH execution_log AND JSONL with zero
    cost. The role's JSONL stays consistent so cross-role rollups don't
    silently lose subprocess-killed runs."""
    trace_id = _seed_start_row(log, ticket_id="jeffrichley/voice#42", action="dispatch_planner")
    recorder.record_subprocess_terminated(
        trace_id=trace_id,
        role="planner",
        repo_slug="jeffrichley/voice",
        ticket_id="jeffrichley/voice#42",
        project="voice",
        issue_number=42,
        exit_outcome="subprocess_timeout",
        duration_seconds=42.0,
    )

    with sqlite3.connect(log.db_path) as conn:
        row = conn.execute(
            "SELECT outcome, total_cost_usd, input_tokens FROM execution_log "
            "WHERE parent_log_id = ?",
            (trace_id,),
        ).fetchone()
    assert row is not None
    outcome, total_cost_usd, input_tokens = row
    assert outcome == "subprocess_killed"
    assert total_cost_usd is None or total_cost_usd == 0
    assert input_tokens == 0 or input_tokens is None

    jsonl_path = tmp_path / "stats" / "jeffrichley__voice" / "planner.jsonl"
    assert jsonl_path.exists()
    line = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[-1])
    assert line["outcome"] == "subprocess_killed"
    assert line["input_tokens"] == 0
    assert line["output_tokens"] == 0
    assert line["total_cost_usd"] is None


# ---------------------------------------------------------------------------
# 4. Subprocess-killed WITH prior completion — dedup catches it (cross-process).
# ---------------------------------------------------------------------------


def test_subprocess_terminated_after_dispatch_complete_is_noop(
    recorder: DispatchRecorder, log: ExecutionLog, tmp_path: Path
) -> None:
    """foreman#251 acceptance criterion: when the role subprocess
    already emitted ``record_dispatch_complete`` (success or in-band
    failure path), the parent's subsequent ``record_subprocess_terminated``
    is a no-op. The existing cost row stays the source of truth.

    The in-process ``_seen`` set handles this directly when the same
    Recorder instance saw both — and the parent's CostSubscriber also
    does a defensive DB lookup so a cross-process emit (subprocess
    Recorder writing the cost row, parent Recorder writing the
    terminate) is still caught."""
    trace_id = _seed_start_row(log, ticket_id="jeffrichley/voice#42", action="dispatch_planner")

    # Simulate the subprocess's emit FIRST.
    recorder.record_dispatch_complete(**_dispatch_complete_kwargs(trace_id=trace_id))  # type: ignore[arg-type]

    rows_before = _count_terminate_rows(log, trace_id)
    jsonl_path = tmp_path / "stats" / "jeffrichley__voice" / "planner.jsonl"
    lines_before = jsonl_path.read_text(encoding="utf-8").splitlines()

    # Now the parent calls terminate. Must be a no-op.
    recorder.record_subprocess_terminated(
        trace_id=trace_id,
        role="planner",
        repo_slug="jeffrichley/voice",
        ticket_id="jeffrichley/voice#42",
        project="voice",
        issue_number=42,
        exit_outcome="success",
        duration_seconds=87.5,
    )

    rows_after = _count_terminate_rows(log, trace_id)
    lines_after = jsonl_path.read_text(encoding="utf-8").splitlines()

    assert rows_before == rows_after == 1
    assert len(lines_before) == len(lines_after) == 1


def test_subprocess_terminated_dedups_when_seen_in_separate_recorder(
    tmp_path: Path, log: ExecutionLog
) -> None:
    """Cross-process dedup: subprocess's Recorder writes the cost row;
    parent's Recorder (a separate instance) calls
    ``record_subprocess_terminated``. The parent has never seen the
    trace_id locally, but the DB lookup on the parent's CostSubscriber
    finds the existing completion row and skips."""
    trace_id = _seed_start_row(log, ticket_id="jeffrichley/voice#42", action="dispatch_planner")
    subprocess_recorder = DispatchRecorder(log=log, stats_root=tmp_path / "stats")
    parent_recorder = DispatchRecorder(log=log, stats_root=tmp_path / "stats")

    subprocess_recorder.record_dispatch_complete(
        **_dispatch_complete_kwargs(trace_id=trace_id)  # type: ignore[arg-type]
    )

    rows_before = _count_terminate_rows(log, trace_id)
    jsonl_path = tmp_path / "stats" / "jeffrichley__voice" / "planner.jsonl"
    lines_before = jsonl_path.read_text(encoding="utf-8").splitlines()

    parent_recorder.record_subprocess_terminated(
        trace_id=trace_id,
        role="planner",
        repo_slug="jeffrichley/voice",
        ticket_id="jeffrichley/voice#42",
        project="voice",
        issue_number=42,
        exit_outcome="success",
        duration_seconds=87.5,
    )

    rows_after = _count_terminate_rows(log, trace_id)
    lines_after = jsonl_path.read_text(encoding="utf-8").splitlines()

    assert rows_before == rows_after == 1
    assert len(lines_before) == len(lines_after) == 1


# ---------------------------------------------------------------------------
# 5. End-to-end: success path writes both ledgers; cost agrees.
# ---------------------------------------------------------------------------


def test_dispatch_complete_writes_both_ledgers_with_matching_cost(
    recorder: DispatchRecorder, log: ExecutionLog, tmp_path: Path
) -> None:
    """Cost in execution_log == cost in JSONL. The dual-write contract
    of Phase 1 is what eliminates the previous one-PR-per-sibling
    cycle; this test pins that the two ledgers agree."""
    trace_id = _seed_start_row(log, ticket_id="jeffrichley/voice#42", action="dispatch_planner")
    recorder.record_dispatch_complete(**_dispatch_complete_kwargs(trace_id=trace_id))  # type: ignore[arg-type]

    with sqlite3.connect(log.db_path) as conn:
        row = conn.execute(
            "SELECT input_tokens, output_tokens, total_cost_usd "
            "FROM execution_log WHERE parent_log_id = ?",
            (trace_id,),
        ).fetchone()
    db_input, db_output, db_cost = row

    jsonl_path = tmp_path / "stats" / "jeffrichley__voice" / "planner.jsonl"
    line = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[-1])
    assert db_input == line["input_tokens"]
    assert db_output == line["output_tokens"]
    assert db_cost == pytest.approx(line["total_cost_usd"])


# ---------------------------------------------------------------------------
# 6. Failure path: dispatch_complete with a failure outcome writes both
# ledgers ONCE each (and a subsequent terminated is dedup'd).
# ---------------------------------------------------------------------------


def test_failure_path_writes_both_ledgers_once_each(
    recorder: DispatchRecorder, log: ExecutionLog, tmp_path: Path
) -> None:
    trace_id = _seed_start_row(log, ticket_id="jeffrichley/voice#42", action="dispatch_planner")
    # foreman#256: legacy "spec_failed" replaced by uniform "exception"
    # (PR #255 / commit b18a683); production code no longer emits
    # the legacy value.
    recorder.record_dispatch_complete(
        **_dispatch_complete_kwargs(  # type: ignore[arg-type]
            trace_id=trace_id, outcome="exception", pr_number=None
        )
    )
    recorder.record_subprocess_terminated(
        trace_id=trace_id,
        role="planner",
        repo_slug="jeffrichley/voice",
        ticket_id="jeffrichley/voice#42",
        project="voice",
        issue_number=42,
        exit_outcome="subprocess_nonzero_exit",
        duration_seconds=87.5,
    )

    rows = _count_terminate_rows(log, trace_id)
    assert rows == 1

    jsonl_path = tmp_path / "stats" / "jeffrichley__voice" / "planner.jsonl"
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["outcome"] == "exception"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_terminate_rows(log: ExecutionLog, trace_id: int) -> int:
    with sqlite3.connect(log.db_path) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM execution_log WHERE parent_log_id = ?",
                (trace_id,),
            ).fetchone()[0]
        )


# ---------------------------------------------------------------------------
# Subscriber-level coverage: CostSubscriber knows about cost columns only;
# RoleStatsSubscriber knows about JSONL only. They are not coupled.
# ---------------------------------------------------------------------------


def test_cost_subscriber_writes_only_to_execution_log(
    log: ExecutionLog, tmp_path: Path
) -> None:
    trace_id = _seed_start_row(log, ticket_id="jeffrichley/voice#42", action="dispatch_planner")
    cost_sub = CostSubscriber(log=log)
    cost_sub.handle_dispatch_complete(
        **_dispatch_complete_kwargs(trace_id=trace_id)  # type: ignore[arg-type]
    )

    assert _count_terminate_rows(log, trace_id) == 1
    # The JSONL file does not exist because CostSubscriber doesn't touch it.
    assert not (tmp_path / "stats").exists() or not any(
        (tmp_path / "stats").rglob("*.jsonl")
    )


def test_role_stats_subscriber_writes_only_to_jsonl(
    log: ExecutionLog, tmp_path: Path
) -> None:
    trace_id = _seed_start_row(log, ticket_id="jeffrichley/voice#42", action="dispatch_planner")
    stats_sub = RoleStatsSubscriber(stats_root=tmp_path / "stats")
    stats_sub.handle_dispatch_complete(
        **_dispatch_complete_kwargs(trace_id=trace_id)  # type: ignore[arg-type]
    )

    assert (tmp_path / "stats" / "jeffrichley__voice" / "planner.jsonl").exists()
    # No new row in execution_log.
    assert _count_terminate_rows(log, trace_id) == 0
