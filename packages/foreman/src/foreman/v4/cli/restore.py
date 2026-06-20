"""foreman restore <snapshot-file> — swap a snapshot into the live DB.

The other half of the issue #360 backup story. The
:class:`~foreman.v4.state_backup.BackupScheduler` writes hourly
snapshots to ``~/.foreman/backups/``; this command swaps one of those
snapshots back into ``config.db_path`` so the operator can recover
from a WSL2 corruption, accidental ``docker compose down -v``, or a
single bad backup-tier window.

Procedure, in order:

1. Resolve ``db_path`` from ``ctx.obj.config.db_path``. Refuse with
   exit 1 when ``config`` is None (matches the
   :mod:`~foreman.v4.cli.mutations` pattern for commands that need
   ``V4Config``).
2. Refuse if the daemon is alive (PID file at
   :data:`~foreman.v4.cli.daemon.PID_PATH` exists AND
   :func:`~foreman.v4.cli.daemon.is_pid_alive` returns True). The
   daemon's writers would race the restore swap and corrupt either
   the live DB or the restored one. **Best-effort caveat**: this
   check is reliable for host-direct invocations (``foreman daemon
   start`` directly on the host); it is BEST-EFFORT inside the
   documented ``docker compose run --rm daemon foreman restore
   <file>`` shape because the one-off container spawns with a fresh
   writable layer and a fresh ``/root/.foreman/v4/`` and literally
   cannot see the daemon container's PID file. The RUNBOOK
   procedure makes ``docker compose stop daemon`` the MANDATORY
   first step before calling restore.
3. Validate ``snapshot_file`` exists and is readable; refuse with
   exit 1 otherwise.
4. Take a "pre-restore" snapshot of the live DB (calls
   :func:`~foreman.v4.state_backup.take_snapshot` against the live
   connection) and rename it to ``foreman.pre-restore-<ts>.sqlite.gz``
   alongside ``db_path``. Print the path to stdout so the operator
   can grep their terminal scrollback for the recovery anchor.
5. If ``snapshot_file.name.endswith(".gz")`` decompress to a
   tempfile alongside ``db_path`` (same dir, so the eventual
   ``os.replace`` is atomic on POSIX); otherwise treat as a raw
   SQLite file and copy it to the tempfile.
6. Open the tempfile with ``sqlite3.connect(tempfile)`` and run
   ``PRAGMA integrity_check;``. If the result is anything other
   than the single row ``("ok",)``, delete the tempfile, print the
   failure detail, exit 1. The pre-restore snapshot from step 4
   stays — the operator can still recover.
7. ``os.replace(tempfile, db_path)`` — atomic on POSIX; on Linux
   this is what the container runs.
8. Print the success message and exit 0.
"""

from __future__ import annotations

import datetime as dt
import gzip
import os
import shutil
import sqlite3
from pathlib import Path

import typer

from foreman.v4.cli import daemon as _daemon_cli
from foreman.v4.state_backup import take_snapshot


def cmd_restore(
    ctx: typer.Context,
    snapshot_file: Path = typer.Argument(...),  # noqa: B008
) -> None:
    """Restore a foreman SQLite snapshot into the live state DB.

    Refuses to run while the daemon is alive (PID-file check, best-
    effort across containers — see module docstring). Takes a
    pre-restore snapshot of the live DB before the swap so the
    operator has a one-step undo. Validates the restored DB passes
    ``PRAGMA integrity_check`` before atomic-replacing the live file.
    """
    config = ctx.obj.config
    if config is None:
        typer.echo("restore requires V4Config", err=True)
        raise typer.Exit(code=1)

    db_path = Path(config.db_path)

    # Step 2: daemon-liveness check. See module docstring on the
    # best-effort caveat under the Docker invocation. Look up
    # ``PID_PATH`` / ``is_pid_alive`` through the daemon CLI module
    # at call time so tests can ``monkeypatch.setattr(
    # "foreman.v4.cli.daemon.PID_PATH", ...)`` and the patched value
    # actually fires here.
    pid_path = _daemon_cli.PID_PATH
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
        except ValueError:
            pid = -1
        if pid > 0 and _daemon_cli.is_pid_alive(pid):
            typer.echo(
                f"daemon is running (pid {pid}); stop it first with "
                "`foreman daemon stop` before restoring",
                err=True,
            )
            raise typer.Exit(code=1)

    # Step 3: validate the snapshot file is reachable.
    if not snapshot_file.exists():
        typer.echo(f"snapshot file not found: {snapshot_file}", err=True)
        raise typer.Exit(code=1)
    if not os.access(snapshot_file, os.R_OK):
        typer.echo(f"snapshot file not readable: {snapshot_file}", err=True)
        raise typer.Exit(code=1)

    now = dt.datetime.now(dt.UTC)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")

    # Step 4: take a pre-restore snapshot of the live DB so the
    # operator has a one-step undo. Reuses take_snapshot for the
    # online-backup semantics — same WAL-safety argument applies.
    # When db_path itself doesn't exist (e.g. operator wiped the
    # state volume and is restoring a snapshot into nothing),
    # skip the pre-restore — there's no live DB to back up.
    pre_restore_path: Path | None = None
    if db_path.exists():
        try:
            live_conn = sqlite3.connect(db_path)
        except sqlite3.Error as exc:
            typer.echo(
                f"could not open live DB for pre-restore: {exc}", err=True,
            )
            raise typer.Exit(code=1) from exc
        try:
            raw_snapshot = take_snapshot(
                live_conn, db_path.parent, now=now,
            )
        finally:
            live_conn.close()
        pre_restore_path = db_path.parent / (
            f"foreman.pre-restore-{timestamp}.sqlite.gz"
        )
        raw_snapshot.rename(pre_restore_path)
        typer.echo(f"pre-restore snapshot saved: {pre_restore_path}")

    # Step 5: decompress or copy the snapshot into a tempfile in the
    # same dir as db_path so ``os.replace`` is atomic on POSIX.
    tempfile = db_path.parent / f".restore-{timestamp}.sqlite.tmp"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if snapshot_file.name.endswith(".gz"):
            with gzip.open(snapshot_file, "rb") as gz_f, open(tempfile, "wb") as out_f:
                shutil.copyfileobj(gz_f, out_f)
        else:
            shutil.copyfile(snapshot_file, tempfile)
    except OSError as exc:
        typer.echo(
            f"could not stage snapshot at {tempfile}: {exc}", err=True,
        )
        raise typer.Exit(code=1) from exc

    # Step 6: run PRAGMA integrity_check on the staged file before
    # replacing the live DB. Anything other than ("ok",) means the
    # staged file is corrupt and must NOT replace the live DB.
    integrity_failed: bool = False
    integrity_detail: str = ""
    try:
        check_conn = sqlite3.connect(tempfile)
        try:
            row = check_conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            check_conn.close()
        if row is None or row[0] != "ok":
            integrity_failed = True
            integrity_detail = "unknown" if row is None else str(row[0])
    except sqlite3.Error as exc:
        integrity_failed = True
        integrity_detail = f"{type(exc).__name__}: {exc}"

    if integrity_failed:
        try:
            tempfile.unlink()
        except FileNotFoundError:
            pass
        typer.echo(
            f"snapshot failed integrity check: {integrity_detail}",
            err=True,
        )
        raise typer.Exit(code=1)

    # Step 7: atomic POSIX replace.
    os.replace(tempfile, db_path)

    # Step 8: success message + how-to-restart pointer.
    if pre_restore_path is None:
        typer.echo(
            f"restored {snapshot_file} -> {db_path} (no pre-restore "
            "snapshot taken — no prior live DB found); start the daemon "
            "with `docker compose up -d daemon`",
        )
    else:
        typer.echo(
            f"restored {snapshot_file} -> {db_path} (pre-restore saved "
            f"as {pre_restore_path}); start the daemon with "
            "`docker compose up -d daemon`",
        )
