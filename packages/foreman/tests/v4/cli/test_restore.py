"""foreman restore <snapshot> — swap a snapshot back into the live DB."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from foreman.v4.cli import app
from foreman.v4.cli.context import build_cli_context
from foreman.v4.config import (
    AppCredentials,
    AppsConfig,
    BackupConfig,
    OperatorConfig,
    OperatorIdentity,
    OrchestratorConfig,
    ProjectConfig,
    V4Config,
)
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.state_backup import take_snapshot

_T0 = dt.datetime(2026, 6, 13, 12, 0, 0, tzinfo=dt.UTC)


def _minimal_v4_config(tmp_path: Path) -> V4Config:
    fake_creds = AppCredentials(app_id=1, private_key_path="/tmp/fake.pem")
    return V4Config(
        db_path=str(tmp_path / "live.db"),
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
            supervisor=OperatorIdentity(name="Sup", email="sup@example.com"),
            signer=OperatorIdentity(name="Sign", email="sign@example.com"),
        ),
        backup=BackupConfig(dir=str(tmp_path / "backups")),
        projects=[
            ProjectConfig(
                name="p", repo="o/p",
                local_clone_path=str(tmp_path / "p"),
            ),
        ],
    )


def _seed_repo(db_path: Path, issue_count: int = 3) -> SqliteTicketRepository:
    """Seed a SqliteTicketRepository on-disk with tickets."""
    repo = SqliteTicketRepository.at_path(db_path)
    for n in range(issue_count):
        ticket = repo.create_ticket(
            project="p", issue_number=n + 1, now=_T0,
        )
        repo.open_state_instance(
            ticket_id=ticket.id, state_name="Planning",
            sequence=1, now=_T0,
        )
    return repo


def test_restore_round_trip(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: seed live DB → take snapshot → delete live DB →
    run ``cmd_restore`` → resulting DB passes integrity check and has
    the original tickets."""
    monkeypatch.setattr(
        "foreman.v4.cli.daemon.PID_PATH", tmp_path / "missing.pid",
    )
    config = _minimal_v4_config(tmp_path)
    db_path = Path(config.db_path)

    repo = _seed_repo(db_path, issue_count=3)
    backups = tmp_path / "backups"
    snapshot_path = take_snapshot(repo.connection, backups, now=_T0)
    # Close the connection so we can replace the file.
    repo.connection.close()

    # Simulate volume loss.
    db_path.unlink()

    result = CliRunner().invoke(
        app, ["restore", str(snapshot_path)],
        obj=build_cli_context(repo=repo, config=config),
    )
    assert result.exit_code == 0, result.output
    assert db_path.exists()

    # Verify the restored DB.
    check_conn = sqlite3.connect(db_path)
    try:
        integrity = check_conn.execute("PRAGMA integrity_check").fetchone()
        assert integrity == ("ok",)
        ticket_count = check_conn.execute(
            "SELECT COUNT(*) FROM tickets",
        ).fetchone()[0]
        assert ticket_count == 3
    finally:
        check_conn.close()


def test_restore_refuses_when_daemon_alive(tmp_path: Path, monkeypatch) -> None:
    """A PID file pointing at a live PID (we use os.getpid()) → refuse.
    Live DB must NOT be modified."""
    pid_path = tmp_path / "daemon.pid"
    pid_path.write_text(str(os.getpid()))
    monkeypatch.setattr("foreman.v4.cli.daemon.PID_PATH", pid_path)

    config = _minimal_v4_config(tmp_path)
    db_path = Path(config.db_path)

    repo = _seed_repo(db_path, issue_count=2)
    backups = tmp_path / "backups"
    snapshot_path = take_snapshot(repo.connection, backups, now=_T0)
    repo.connection.close()

    db_bytes_before = db_path.read_bytes()

    result = CliRunner().invoke(
        app, ["restore", str(snapshot_path)],
        obj=build_cli_context(repo=repo, config=config),
    )
    assert result.exit_code == 1
    assert "daemon is running" in result.output
    # Live DB UNCHANGED.
    assert db_path.read_bytes() == db_bytes_before


def test_restore_refuses_corrupt_snapshot(tmp_path: Path, monkeypatch) -> None:
    """A hand-crafted "snapshot" of random bytes must be rejected. The
    pre-restore snapshot from step 4 stays on disk as the recovery
    anchor. The live DB is NOT modified."""
    monkeypatch.setattr(
        "foreman.v4.cli.daemon.PID_PATH", tmp_path / "missing.pid",
    )
    config = _minimal_v4_config(tmp_path)
    db_path = Path(config.db_path)

    repo = _seed_repo(db_path, issue_count=2)
    repo.connection.close()

    corrupt = tmp_path / "fake.sqlite.gz"
    corrupt.write_bytes(b"\x1f\x8b\x08this-is-not-a-real-gzipped-sqlite")

    db_bytes_before = db_path.read_bytes()

    result = CliRunner().invoke(
        app, ["restore", str(corrupt)],
        obj=build_cli_context(repo=repo, config=config),
    )
    assert result.exit_code == 1
    # Live DB is NOT modified.
    assert db_path.read_bytes() == db_bytes_before

    # Pre-restore snapshot saved as recovery anchor.
    pre_restore_candidates = list(
        db_path.parent.glob("foreman.pre-restore-*.sqlite.gz"),
    )
    assert pre_restore_candidates, (
        "pre-restore snapshot must exist on disk after a failed restore"
    )
