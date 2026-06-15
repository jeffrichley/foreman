"""daemon status — read PID file, report state."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.sqlite_repository import SqliteTicketRepository


def test_status_when_no_pid_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "foreman.v4.cli.daemon._PID_PATH", tmp_path / "missing.pid",
    )
    result = CliRunner().invoke(
        app, ["daemon", "status"],
        obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
    )
    assert "not running" in result.output


def test_status_when_pid_alive(tmp_path: Path, monkeypatch):
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("12345")
    monkeypatch.setattr("foreman.v4.cli.daemon._PID_PATH", pid_path)
    with patch("os.kill") as mock_kill:
        mock_kill.return_value = None
        result = CliRunner().invoke(
            app, ["daemon", "status"],
            obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
        )
    assert "running" in result.output
    assert "12345" in result.output


def test_status_when_pid_stale(tmp_path: Path, monkeypatch):
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("99999")
    monkeypatch.setattr("foreman.v4.cli.daemon._PID_PATH", pid_path)
    with patch("os.kill", side_effect=ProcessLookupError):
        result = CliRunner().invoke(
            app, ["daemon", "status"],
            obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
        )
    assert "stale" in result.output
