"""Tests for the ``foreman restore`` CLI command (foreman#434)."""
from __future__ import annotations

import gzip
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from foreman.v4.cli import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path, dsn: str = "postgresql://foreman:pw@localhost:5432/foreman") -> MagicMock:
    """Build a minimal mock config for CliContext.obj."""
    config = MagicMock()
    config.storage.dsn = dsn
    config.backup.dir = str(tmp_path / "backups")
    return config


def _make_ctx(config: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.config = config
    return ctx


# ---------------------------------------------------------------------------
# test_restore_calls_psql_with_correct_args
# ---------------------------------------------------------------------------

def test_restore_calls_psql_with_correct_args(tmp_path: Path) -> None:
    """Happy path: restore a .sql file, psql called with correct args."""
    dsn = "postgresql://foreman:pw@localhost:5432/foreman"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    # A plain .sql snapshot (no gz suffix → copy path)
    snapshot = tmp_path / "foreman-20260627T120000Z.sql"
    snapshot.write_text("-- PostgreSQL database dump\n")

    config = _make_config(tmp_path, dsn=dsn)
    config.backup.dir = str(backup_dir)
    ctx = _make_ctx(config)

    psql_calls: list = []

    def fake_run(args, **kwargs):
        psql_calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    # Pre-restore pg_dump mock
    def fake_take_snapshot(*, dsn, dst_dir, now):
        pre_restore_file = dst_dir / f"foreman-{now.strftime('%Y%m%dT%H%M%SZ')}.sql.gz"
        pre_restore_file.parent.mkdir(parents=True, exist_ok=True)
        pre_restore_file.write_bytes(gzip.compress(b"-- pre-restore dump"))
        return pre_restore_file

    runner = CliRunner()
    with patch("foreman.v4.cli.restore.take_snapshot", side_effect=fake_take_snapshot):
        with patch("foreman.v4.cli.restore.subprocess.run", side_effect=fake_run):
            with patch("foreman.v4.cli.restore.PID_PATH", tmp_path / "daemon.pid"):
                result = runner.invoke(
                    app,
                    ["restore", str(snapshot)],
                    obj=ctx,
                    catch_exceptions=False,
                )

    assert result.exit_code == 0, f"Expected exit 0; got {result.exit_code}\n{result.output}"
    assert len(psql_calls) == 1
    psql_cmd = psql_calls[0]
    assert psql_cmd[0] == "psql"
    assert psql_cmd[1] == dsn
    assert "--file" in psql_cmd
    assert "--quiet" in psql_cmd


# ---------------------------------------------------------------------------
# test_restore_refuses_when_daemon_alive
# ---------------------------------------------------------------------------

def test_restore_refuses_when_daemon_alive(tmp_path: Path) -> None:
    """Exit 1 and no psql call when a live daemon PID file exists."""
    dsn = "postgresql://foreman:pw@localhost:5432/foreman"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    snapshot = tmp_path / "foreman-20260627T120000Z.sql"
    snapshot.write_text("-- dump")

    # Write a PID file pointing at our own process (guaranteed alive)
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text(str(os.getpid()))

    config = _make_config(tmp_path, dsn=dsn)
    config.backup.dir = str(backup_dir)
    ctx = _make_ctx(config)

    runner = CliRunner()
    with patch("foreman.v4.cli.restore.PID_PATH", pid_file):
        with patch("foreman.v4.cli.restore.subprocess.run") as mock_run:
            result = runner.invoke(
                app,
                ["restore", str(snapshot)],
                obj=ctx,
            )

    assert result.exit_code == 1, f"Expected exit 1; got {result.exit_code}\n{result.output}"
    mock_run.assert_not_called()
    assert "stop it first" in result.output or "daemon is running" in result.output


# ---------------------------------------------------------------------------
# test_restore_refuses_missing_snapshot
# ---------------------------------------------------------------------------

def test_restore_refuses_missing_snapshot(tmp_path: Path) -> None:
    """Exit 1 when the snapshot file doesn't exist."""
    dsn = "postgresql://foreman:pw@localhost:5432/foreman"
    config = _make_config(tmp_path, dsn=dsn)
    ctx = _make_ctx(config)

    non_existent = tmp_path / "ghost.sql.gz"

    runner = CliRunner()
    with patch("foreman.v4.cli.restore.PID_PATH", tmp_path / "no-pid.pid"):
        with patch("foreman.v4.cli.restore.subprocess.run") as mock_run:
            result = runner.invoke(
                app,
                ["restore", str(non_existent)],
                obj=ctx,
            )

    assert result.exit_code == 1
    mock_run.assert_not_called()
