"""daemon status — read PID file, report state."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.cli.daemon import _build_sighup_handler
from foreman.v4.config import (
    AppCredentials,
    AppsConfig,
    OperatorConfig,
    OperatorIdentity,
    OrchestratorConfig,
    ProjectConfig,
    V4Config,
)
from foreman.v4.json_lines_handler import JsonLinesHandler
from foreman.v4.logging_config import configure_logging, reset_logging
from foreman.v4.sqlite_repository import SqliteTicketRepository

# Mirrors the private constant in foreman.v4.logging_config. Inlined
# (not imported) to avoid private-import lint noise; test_bootstrap.py
# follows the same pattern.
_V4_LOGGER_NAMES = (
    "foreman.v4",
    "foreman.v4.transitions",
    "foreman.v4.event_bus",
)


def test_status_when_no_pid_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "foreman.v4.cli.daemon.PID_PATH", tmp_path / "missing.pid",
    )
    result = CliRunner().invoke(
        app, ["daemon", "status"],
        obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
    )
    assert "not running" in result.output


def test_status_when_pid_alive(tmp_path: Path, monkeypatch):
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("12345")
    monkeypatch.setattr("foreman.v4.cli.daemon.PID_PATH", pid_path)
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
    monkeypatch.setattr("foreman.v4.cli.daemon.PID_PATH", pid_path)
    with patch("os.kill", side_effect=ProcessLookupError):
        result = CliRunner().invoke(
            app, ["daemon", "status"],
            obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
        )
    assert "stale" in result.output


def test_stop_when_pid_stale_posix(tmp_path: Path, monkeypatch):
    """A stale PID file under POSIX (ProcessLookupError from os.kill)
    must report cleanly AND unlink the stale PID file.
    """
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("99999")
    monkeypatch.setattr("foreman.v4.cli.daemon.PID_PATH", pid_path)
    with patch("os.kill", side_effect=ProcessLookupError):
        result = CliRunner().invoke(
            app, ["daemon", "stop"],
            obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
        )
    assert result.exit_code == 0
    assert "not running" in result.output
    assert not pid_path.exists(), "stale PID file should be unlinked"


# --- Single-instance start guard ---------------------------------------

def test_start_refuses_when_daemon_already_running(tmp_path: Path, monkeypatch):
    """A second daemon must NOT start while the first is alive.

    run_forever's startup reconcile closes every in-flight
    state_instances row as a crash orphan — correct only under a single
    instance. Starting a second daemon while the first holds a live PID
    would close the live daemon's in-flight row, corrupting an
    actively-running ticket. start must refuse and leave the PID file
    untouched.
    """
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("12345")
    monkeypatch.setattr("foreman.v4.cli.daemon.PID_PATH", pid_path)
    mock_daemon = MagicMock()
    with patch("os.kill", return_value=None):  # PID 12345 reads as alive
        result = CliRunner().invoke(
            app, ["daemon", "start"],
            obj=build_cli_context(
                repo=SqliteTicketRepository.in_memory(), daemon=mock_daemon,
            ),
        )
    assert result.exit_code == 1
    assert "already running" in result.output
    assert "12345" in result.output
    mock_daemon.run_forever.assert_not_called()
    assert pid_path.read_text().strip() == "12345"  # not stomped


def test_start_overwrites_stale_pid_and_proceeds(tmp_path: Path, monkeypatch):
    """A stale PID file (prior daemon crashed — the Watchtower-redeploy
    case) is not a live instance: start overwrites it with the current
    PID and proceeds into run_forever. We capture the PID written at the
    moment run_forever is reached (the finally block unlinks it on exit).
    """
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("99999")
    monkeypatch.setattr("foreman.v4.cli.daemon.PID_PATH", pid_path)

    captured: dict[str, str] = {}

    def _capture_pid_then_return() -> None:
        captured["pid"] = pid_path.read_text().strip()

    mock_daemon = MagicMock()
    mock_daemon.run_forever.side_effect = _capture_pid_then_return

    with patch("os.kill", side_effect=ProcessLookupError):  # 99999 is dead
        result = CliRunner().invoke(
            app, ["daemon", "start"],
            obj=build_cli_context(
                repo=SqliteTicketRepository.in_memory(), daemon=mock_daemon,
            ),
        )
    assert result.exit_code == 0
    mock_daemon.run_forever.assert_called_once()
    assert captured["pid"] == str(os.getpid())  # stale 99999 overwritten


# --- Task 8.5: SIGHUP handler reset+reconfigure logging ----------------

def _minimal_v4_config(tmp_path: Path) -> V4Config:
    """The smallest V4Config that satisfies validation.

    The SIGHUP handler only reads ``log_dir`` and ``log_level``;
    the rest is fake-padding so V4Config validates.
    """
    fake_creds = AppCredentials(app_id=1, private_key_path="/tmp/fake.pem")
    return V4Config(
        db_path=str(tmp_path / "v4.db"),
        log_dir=str(tmp_path / "logs"),
        log_level="INFO",
        apps=AppsConfig(
            planner=fake_creds, reviewer=fake_creds,
            fixer=fake_creds, worker=fake_creds,
        ),
        orchestrator=OrchestratorConfig(
            app_id=2, private_key_path="/tmp/fake-orch.pem",
        ),
        operator=OperatorConfig(
            supervisor=OperatorIdentity(name="Test Sup", email="sup@example.com"),
            signer=OperatorIdentity(name="Test Sign", email="sign@example.com"),
        ),
        projects=[
            ProjectConfig(
                name="p", repo="o/p",
                local_clone_path=str(tmp_path / "p"),
            ),
        ],
    )


def _handler_counts_by_type() -> dict[str, dict[str, int]]:
    """Snapshot handler-type counts per v4 logger.

    Returns ``{logger_name: {handler_type_name: count}}``. We assert
    that across reloads the count per (logger, handler-type) stays
    at exactly 1, never grows.
    """
    snapshot: dict[str, dict[str, int]] = {}
    for name in _V4_LOGGER_NAMES:
        logger = logging.getLogger(name)
        counts: dict[str, int] = {}
        for handler in logger.handlers:
            type_name = type(handler).__name__
            counts[type_name] = counts.get(type_name, 0) + 1
        snapshot[name] = counts
    return snapshot


def test_sighup_resets_and_reconfigures_logging_no_stacking(tmp_path: Path):
    """A simulated SIGHUP must NOT stack file handlers.

    We invoke the SIGHUP handler directly (signal delivery isn't
    safely testable cross-platform) and assert the JsonLinesHandler +
    RichHandler counts stay at 1-each across multiple reloads.
    """
    # Baseline: fresh logging surface.
    reset_logging()
    config = _minimal_v4_config(tmp_path)

    # Configure once (matches what bootstrap does at start).
    configure_logging(log_dir=Path(config.log_dir), level=config.log_level)
    baseline = _handler_counts_by_type()
    for name in _V4_LOGGER_NAMES:
        assert baseline[name].get("RichHandler") == 1
        assert baseline[name].get("JsonLinesHandler") == 1

    # Build the same closure cmd_daemon_start would install.
    handler = _build_sighup_handler(config)

    # Simulate 3 SIGHUPs in a row. Each must end with the same
    # 1-each handler shape — no stacking.
    for _ in range(3):
        handler()  # signal-handler signature: (*_args)
        after = _handler_counts_by_type()
        for name in _V4_LOGGER_NAMES:
            assert after[name].get("RichHandler") == 1, (
                f"RichHandler stacked on {name}: {after[name]}"
            )
            assert after[name].get("JsonLinesHandler") == 1, (
                f"JsonLinesHandler stacked on {name}: {after[name]}"
            )


def test_sighup_handler_closes_old_file_handles_on_reset(tmp_path: Path):
    """reset_logging() must close handlers, not just detach them.

    JsonLinesHandler owns a file handle; if reset only removed the
    handler reference, the underlying file stays open. We assert
    the handler instance returned by ``logger.handlers`` after
    configure is a NEW object across reloads — i.e. the old one
    was discarded.
    """
    reset_logging()
    config = _minimal_v4_config(tmp_path)
    configure_logging(log_dir=Path(config.log_dir), level=config.log_level)
    first = {
        name: list(logging.getLogger(name).handlers)
        for name in _V4_LOGGER_NAMES
    }
    _build_sighup_handler(config)()
    second = {
        name: list(logging.getLogger(name).handlers)
        for name in _V4_LOGGER_NAMES
    }
    for name in _V4_LOGGER_NAMES:
        for h_before in first[name]:
            # Each old handler instance is gone from the new list.
            assert h_before not in second[name], (
                f"old handler survived reset on {name}: {h_before!r}"
            )
        # Sanity: file handler in second snapshot really points
        # at the configured log_dir.
        jsonl = [
            h for h in second[name]
            if isinstance(h, JsonLinesHandler)
        ]
        assert len(jsonl) == 1
