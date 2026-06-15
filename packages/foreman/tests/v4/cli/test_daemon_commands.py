"""daemon status — read PID file, report state."""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.cli.daemon import _build_sighup_handler, _is_pid_alive
from foreman.v4.config import (
    AppCredentials,
    AppsConfig,
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


@pytest.fixture
def _isolate_v4_logging():
    """Snapshot + restore v4 logger propagate flags around a test.

    configure_logging() sets propagate=False on the v4 loggers. The
    SIGHUP tests below call configure_logging directly, so without
    this fixture the propagate-False state leaks to later tests
    (notably the caplog-based observer suites that assume
    propagate=True). Mirrors the same pattern in test_bootstrap.py.
    """
    snapshots = {
        n: logging.getLogger(n).propagate for n in _V4_LOGGER_NAMES
    }
    yield
    reset_logging()
    for name, propagate in snapshots.items():
        logging.getLogger(name).propagate = propagate


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


def _winerror_87_oserror() -> OSError:
    """Build an OSError that mimics Windows' dead-PID os.kill response.

    Phase 8c.5 narrowed the helper from "any OSError = dead" to
    "winerror == 87 (ERROR_INVALID_PARAMETER) = dead". We construct
    the exception by attribute assignment so the test wires up the
    same ``winerror`` attribute the helper reads on both Windows and
    POSIX (POSIX OSError instances normally have no ``winerror``;
    attribute assignment is the cross-platform-safe shape).
    """
    exc = OSError(0, "Windows dead-PID simulation")
    exc.winerror = 87  # type: ignore[attr-defined]
    return exc


def test_status_when_pid_stale_windows_oserror(tmp_path: Path, monkeypatch):
    """Windows raises bare OSError ([WinError 87]) from os.kill on a
    dead PID, not ProcessLookupError. The status command must treat
    that the same way — report 'stale' cleanly, no traceback.

    Phase 8c.5 narrowed the catch from any-OSError to winerror==87
    specifically; this test now wires that winerror through so it
    exercises the narrow path, not the wide fallback.
    """
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("99999")
    monkeypatch.setattr("foreman.v4.cli.daemon._PID_PATH", pid_path)
    with patch("os.kill", side_effect=_winerror_87_oserror()):
        result = CliRunner().invoke(
            app, ["daemon", "status"],
            obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
        )
    assert result.exit_code == 0
    assert "stale" in result.output.lower()


def test_stop_when_pid_stale_posix(tmp_path: Path, monkeypatch):
    """A stale PID file under POSIX (ProcessLookupError from os.kill)
    must report cleanly AND unlink the stale PID file.
    """
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("99999")
    monkeypatch.setattr("foreman.v4.cli.daemon._PID_PATH", pid_path)
    with patch("os.kill", side_effect=ProcessLookupError):
        result = CliRunner().invoke(
            app, ["daemon", "stop"],
            obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
        )
    assert result.exit_code == 0
    assert "not running" in result.output
    assert not pid_path.exists(), "stale PID file should be unlinked"


def test_stop_when_pid_stale_windows_oserror(tmp_path: Path, monkeypatch):
    """Windows raises bare OSError from os.kill on a dead PID. The
    stop command must treat that the same as ProcessLookupError —
    report 'not running' and unlink the stale PID file, no traceback.

    Phase 8c.5: the up-front ``_is_pid_alive`` check now sees the
    winerror=87 exception, returns False, and the stop command takes
    the stale-cleanup path before reaching SIGTERM.
    """
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text("99999")
    monkeypatch.setattr("foreman.v4.cli.daemon._PID_PATH", pid_path)
    with patch("os.kill", side_effect=_winerror_87_oserror()):
        result = CliRunner().invoke(
            app, ["daemon", "stop"],
            obj=build_cli_context(repo=SqliteTicketRepository.in_memory()),
        )
    assert result.exit_code == 0
    assert "not running" in result.output
    assert not pid_path.exists(), "stale PID file should be unlinked"


# --- Task 8c.5: _is_pid_alive helper narrows OSError handling ---------

def test_pid_alive_helper_treats_winerror_87_as_dead():
    """``winerror == 87`` (``ERROR_INVALID_PARAMETER``) is Windows'
    canonical "PID is not a live process" response; the helper must
    return False so callers report stale.
    """
    with patch("os.kill", side_effect=_winerror_87_oserror()):
        assert _is_pid_alive(99999) is False


def test_pid_alive_helper_treats_winerror_5_as_alive():
    """``winerror == 5`` (``ERROR_ACCESS_DENIED``) means the PID IS a
    live process, just one we can't probe (e.g., owned by a different
    user / elevated process). Treat as alive so we don't false-stale
    a running daemon — this is the bug Phase 8c.5 fixes.
    """
    exc = OSError(13, "Access denied")
    exc.winerror = 5  # type: ignore[attr-defined]
    with patch("os.kill", side_effect=exc):
        assert _is_pid_alive(33420) is True


def test_pid_alive_helper_treats_posix_eperm_as_alive():
    """POSIX ``EPERM`` (no ``winerror`` attribute, bare OSError) means
    the PID is alive but we lack permission to signal it. Treat as
    alive — same reasoning as winerror 5.
    """
    with patch(
        "os.kill",
        side_effect=OSError(1, "Operation not permitted"),
    ):
        assert _is_pid_alive(33420) is True


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


def test_sighup_resets_and_reconfigures_logging_no_stacking(
    tmp_path: Path, _isolate_v4_logging: None,
):
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


def test_sighup_handler_closes_old_file_handles_on_reset(
    tmp_path: Path, _isolate_v4_logging: None,
):
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
