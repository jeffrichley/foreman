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


def test_payload_usage_field_is_optional() -> None:
    """foreman#251 (Phase 1): ``usage`` is optional — heartbeats and
    other non-cost writes leave it None."""
    payload = ExecutionLogWritePayload(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        action="worker_heartbeat",
        outcome="running",
        details={"progress": "8/8"},
    )
    assert payload.usage is None


def test_payload_accepts_usage_info() -> None:
    """foreman#251: when present, ``usage`` carries the eight provider-
    reported fields per :class:`UsageInfo`."""
    from foreman.provider import UsageInfo

    payload = ExecutionLogWritePayload(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        action="dispatch_planner",
        outcome="spec_written",
        details={},
        parent_log_id=1,
        usage=UsageInfo(
            input_tokens=1234,
            output_tokens=567,
            total_cost_usd=0.99,
        ),
    )
    assert payload.usage is not None
    assert payload.usage.input_tokens == 1234


def test_handle_envelope_writes_cost_columns_when_usage_present(tmp_path: Path) -> None:
    """foreman#251 (Phase 1): when the payload carries ``usage``, the
    bus endpoint translates it into the new execution_log cost
    columns via :func:`_usage_to_columns`."""
    import json
    import sqlite3

    from foreman.provider import UsageInfo

    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    start_id = log.write_action(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )

    payload = ExecutionLogWritePayload(
        ticket_id="jeffrichley/foreman#143",
        project="foreman",
        action="dispatch_planner",
        outcome="spec_written",
        details={},
        parent_log_id=start_id,
        usage=UsageInfo(
            input_tokens=1234,
            output_tokens=567,
            cache_creation_input_tokens=89,
            cache_read_input_tokens=10,
            total_cost_usd=0.42,
            model_usage={"claude-sonnet": {"input_tokens": 1234}},
            duration_ms=9876,
            num_turns=5,
        ),
    )
    row_id = handle_envelope(payload, log=log)

    with sqlite3.connect(tmp_path / "log.sqlite") as conn:
        row = conn.execute(
            """
            SELECT input_tokens, output_tokens, cache_creation_input_tokens,
                   cache_read_input_tokens, total_cost_usd, model_usage_json,
                   duration_ms, num_turns
            FROM execution_log WHERE id = ?
            """,
            (row_id,),
        ).fetchone()
    assert row[0] == 1234
    assert row[1] == 567
    assert row[2] == 89
    assert row[3] == 10
    assert row[4] == pytest.approx(0.42)
    assert json.loads(row[5]) == {"claude-sonnet": {"input_tokens": 1234}}
    assert row[6] == 9876
    assert row[7] == 5


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
