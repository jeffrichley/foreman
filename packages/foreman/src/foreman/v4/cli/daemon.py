"""daemon start/stop/reload/status — lifecycle commands.

start runs the prepared Daemon in the foreground; writes a PID file at
``~/.foreman/v4/daemon.pid`` so stop/reload/status can find it. SIGTERM
+ SIGINT handlers are installed here (NOT in the Daemon class) — keeps
the Daemon agnostic to process-level concerns so tests don't have to
mock around signal handlers.

reload uses SIGHUP, which doesn't exist on Windows. The guard below
keeps the module importable cross-platform and the CLI exits 1 cleanly
when reload is asked on a host that can't deliver it.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

import typer

_PID_PATH = Path.home() / ".foreman" / "v4" / "daemon.pid"

# SIGHUP doesn't exist on Windows (signal module omits it). getattr
# (instead of `try: signal.SIGHUP`) keeps mypy happy on Windows hosts —
# the `try` form trips Module-has-no-attribute. Resolved once at import
# time so cmd_daemon_reload can fast-fail cleanly when reload is asked
# on a host that can't deliver it.
_SIGHUP: int | None = getattr(signal, "SIGHUP", None)


def cmd_daemon_start(ctx: typer.Context) -> None:
    """Start the daemon in the foreground.

    Tests inject the prepared Daemon via build_cli_context(daemon=...).
    Production wiring (Phase 7) builds the Daemon from config and feeds
    it into build_cli_context the same way.
    """
    daemon = ctx.obj.daemon
    if daemon is None:
        typer.echo("daemon not configured", err=True)
        raise typer.Exit(code=1)
    _PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PID_PATH.write_text(str(os.getpid()))
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_args: daemon.stop())
        daemon.run_forever()
    finally:
        if _PID_PATH.exists():
            _PID_PATH.unlink()


def cmd_daemon_stop(ctx: typer.Context) -> None:
    if not _PID_PATH.exists():
        typer.echo("no daemon PID file", err=True)
        raise typer.Exit(code=1)
    pid = int(_PID_PATH.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        typer.echo(f"PID {pid} not running; cleaning stale file")
        _PID_PATH.unlink()
        return
    typer.echo(f"sent SIGTERM to {pid}")


def cmd_daemon_status(ctx: typer.Context) -> None:
    if not _PID_PATH.exists():
        typer.echo("daemon: not running")
        return
    pid = int(_PID_PATH.read_text().strip())
    try:
        os.kill(pid, 0)
        typer.echo(f"daemon: running (pid {pid})")
    except ProcessLookupError:
        typer.echo(f"daemon: stale PID file (pid {pid} not alive)")


def cmd_daemon_reload(ctx: typer.Context) -> None:
    if _SIGHUP is None:
        typer.echo("reload not supported on this platform", err=True)
        raise typer.Exit(code=1)
    if not _PID_PATH.exists():
        typer.echo("no daemon PID file", err=True)
        raise typer.Exit(code=1)
    pid = int(_PID_PATH.read_text().strip())
    os.kill(pid, _SIGHUP)
    typer.echo(f"sent SIGHUP to {pid}")
