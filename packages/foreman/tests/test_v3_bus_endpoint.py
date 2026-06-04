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
