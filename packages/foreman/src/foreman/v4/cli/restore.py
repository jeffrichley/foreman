"""``foreman restore`` — single-command Postgres snapshot recovery (foreman#434).

Behavior (in order):
  1. Validate config is available (FOREMAN_V4_CONFIG set).
  2. Get the DSN from config.storage.dsn.
  3. Best-effort daemon liveness check via PID_PATH / is_pid_alive.
  4. Validate the snapshot file exists.
  5. Take a pre-restore pg_dump and rename to pre-restore-<ts>.sql.gz.
  6. Decompress (.gz) or copy (.sql) snapshot to a tempfile.
  7. Run psql --file <tempfile> to restore.
  8. Print success message and exit 0.

The liveness check is best-effort — inside a ``docker compose run --rm daemon``
one-off container the daemon's PID file lives in the daemon container's writable
layer (not on a shared volume) so the check can't see it. ``docker compose stop
daemon`` is MANDATORY before invoking restore inside Docker.
"""

from __future__ import annotations

import datetime as dt
import gzip
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from foreman.v4.cli.daemon import PID_PATH, is_pid_alive
from foreman.v4.pg_backup import _dsn_without_password, _subprocess_pg_env, take_snapshot


def cmd_restore(
    ctx: typer.Context,
    snapshot_file: Annotated[
        Path,
        typer.Argument(help="Path to .sql.gz or .sql snapshot file to restore"),
    ],
) -> None:
    """Restore the Postgres database from a pg_dump snapshot.

    Takes a pre-restore backup of the live database, then pipes the given
    snapshot through psql. The pre-restore dump is saved alongside the
    backups so the operation can be reversed with another ``foreman restore``.

    IMPORTANT: stop the daemon first (``docker compose stop daemon``). The
    postgres sidecar must remain running.
    """
    # 1. Validate config is available.
    config = ctx.obj.config if ctx.obj else None
    if config is None:
        typer.echo(
            "no config; ensure FOREMAN_V4_CONFIG is set",
            err=True,
        )
        raise typer.Exit(code=1)

    # 2. Get the DSN.
    dsn = config.storage.dsn
    assert dsn is not None, "StorageConfig validator guarantees a non-None DSN"

    # 3. Best-effort daemon liveness check.
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
        except ValueError:
            pid = None
        if pid is not None and is_pid_alive(pid):
            typer.echo(
                f"daemon is running (pid {pid}); stop it first with `foreman daemon stop`",
                err=True,
            )
            raise typer.Exit(code=1)

    # 4. Validate the snapshot file exists.
    if not snapshot_file.exists() or not snapshot_file.is_file():
        typer.echo(
            f"snapshot file not found or not a file: {snapshot_file}",
            err=True,
        )
        raise typer.Exit(code=1)

    # 5. Take a pre-restore pg_dump.
    now = dt.datetime.now(dt.UTC)
    backup_dir = Path(config.backup.dir)
    try:
        raw_pre_restore = take_snapshot(dsn=dsn, dst_dir=backup_dir, now=now)
    except (OSError, subprocess.SubprocessError) as exc:
        typer.echo(
            f"pre-restore backup failed: {exc}; aborting before any changes",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    pre_restore_path = backup_dir / f"pre-restore-{now.strftime('%Y%m%dT%H%M%SZ')}.sql.gz"
    raw_pre_restore.rename(pre_restore_path)
    typer.echo(f"pre-restore backup saved as {pre_restore_path}")

    # 6. Decompress or copy to a temporary file, then restore.
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".sql")
    tmp_path = Path(tmp_name)
    try:
        os.close(tmp_fd)  # close the fd; we'll write via Path

        suffix = snapshot_file.suffix.lower()
        if suffix == ".gz":
            tmp_path.write_bytes(gzip.decompress(snapshot_file.read_bytes()))
        else:
            # Plain .sql (or any non-gz): copy as-is.
            shutil.copy2(snapshot_file, tmp_path)

        # 7. Run psql.
        # Password is passed via PGPASSWORD env (not argv) to avoid ps exposure.
        # --single-transaction + -v ON_ERROR_STOP=1 make the restore atomic:
        # a truncated/malformed snapshot rolls back instead of leaving the DB
        # half-dropped (foreman Reviewer finding M1).
        safe_dsn = _dsn_without_password(dsn)
        pg_env = _subprocess_pg_env(dsn)
        try:
            subprocess.run(
                [
                    "psql",
                    safe_dsn,
                    "--file", str(tmp_path),
                    "--quiet",
                    "--single-transaction",
                    "-v", "ON_ERROR_STOP=1",
                ],
                check=True,
                env=pg_env,
            )
        except subprocess.CalledProcessError as exc:
            typer.echo(f"psql restore failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    # 8. Success.
    typer.echo(
        f"restored {snapshot_file}; "
        f"pre-restore saved as {pre_restore_path}; "
        "start daemon with `docker compose up -d daemon`"
    )
