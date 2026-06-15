"""log — recent + filtered JSON-lines view."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.sqlite_repository import SqliteTicketRepository


def _write_log(path: Path, lines: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def test_log_prints_recent_lines(tmp_path: Path):
    log_path = tmp_path / "transitions.jsonl"
    _write_log(log_path, [
        {"event": "state_entered", "ticket_id": 1, "state": "Planning",
         "at": "2026-06-13T12:00:00"},
        {"event": "execute_completed", "ticket_id": 1, "state": "Planning",
         "outcome_kind": "clean", "at": "2026-06-13T12:01:00"},
    ])
    runner = CliRunner()
    result = runner.invoke(
        app, ["log", "--log-path", str(log_path)],
        obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
    )
    assert result.exit_code == 0
    assert "state_entered" in result.output
    assert "Planning" in result.output


def test_log_filter_by_ticket(tmp_path: Path):
    log_path = tmp_path / "transitions.jsonl"
    _write_log(log_path, [
        {"event": "state_entered", "ticket_id": 1, "state": "Planning"},
        {"event": "state_entered", "ticket_id": 2, "state": "SpecReview"},
    ])
    runner = CliRunner()
    result = runner.invoke(
        app, ["log", "--log-path", str(log_path), "--ticket", "1"],
        obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
    )
    assert "Planning" in result.output
    assert "SpecReview" not in result.output


def test_log_filter_by_state(tmp_path: Path):
    log_path = tmp_path / "transitions.jsonl"
    _write_log(log_path, [
        {"event": "state_entered", "ticket_id": 1, "state": "Planning"},
        {"event": "state_entered", "ticket_id": 2, "state": "Merging"},
    ])
    runner = CliRunner()
    result = runner.invoke(
        app, ["log", "--log-path", str(log_path), "--state", "Merging"],
        obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
    )
    assert "Merging" in result.output
    assert "Planning" not in result.output


def test_log_limit_caps_output(tmp_path: Path):
    log_path = tmp_path / "transitions.jsonl"
    _write_log(log_path, [
        {"event": "state_entered", "ticket_id": i, "state": "Planning"}
        for i in range(100)
    ])
    runner = CliRunner()
    result = runner.invoke(
        app, ["log", "--log-path", str(log_path), "--limit", "5"],
        obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
    )
    assert result.output.count("state_entered") == 5
