"""daemon start/stop/reload/status — lifecycle commands.

start runs the prepared Daemon in the foreground; writes a PID file at
``~/.foreman/v4/daemon.pid`` so stop/reload/status can find it. SIGTERM
+ SIGINT handlers are installed here (NOT in the Daemon class) — keeps
the Daemon agnostic to process-level concerns so tests don't have to
mock around signal handlers.

reload uses SIGHUP, which doesn't exist on Windows. The guard below
keeps the module importable cross-platform and the CLI exits 1 cleanly
when reload is asked on a host that can't deliver it.

SIGHUP receiver (foreground-start case): the start command installs a
handler that calls ``reset_logging()`` + ``configure_logging(...)``.
This prevents file/Rich handlers from stacking on repeated reloads.
The handler captures ``log_dir`` + ``log_level`` from the V4Config
that was used at daemon start; it does NOT re-read config from disk
(that's a separate concern; this task is handler-stacking prevention).
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from foreman.v4.logging_config import configure_logging, reset_logging

if TYPE_CHECKING:
    from foreman.v4.config import V4Config

_PID_PATH = Path.home() / ".foreman" / "v4" / "daemon.pid"

# SIGHUP doesn't exist on Windows (signal module omits it). getattr
# (instead of `try: signal.SIGHUP`) keeps mypy happy on Windows hosts —
# the `try` form trips Module-has-no-attribute. Resolved once at import
# time so cmd_daemon_reload can fast-fail cleanly when reload is asked
# on a host that can't deliver it.
_SIGHUP: int | None = getattr(signal, "SIGHUP", None)


def _is_pid_alive(pid: int) -> bool:
    """Probe whether ``pid`` corresponds to a live process.

    POSIX semantics: ``os.kill(pid, 0)`` succeeds for a live PID, raises
    ``ProcessLookupError`` for a dead PID, and may raise other OSError
    subtypes (e.g. EPERM) for a live PID we lack permission to signal —
    we let those bubble up rather than swallow them, because misreading
    them as "dead" would falsely report a running daemon as stale.

    Windows-native dev caveat: ``os.kill(pid, 0)`` is not a real Win32
    API — every PID raises ``OSError`` with ``winerror == 87``
    (``ERROR_INVALID_PARAMETER``), alive or dead. Production runs in
    Docker (POSIX), so this helper is correct where it matters; the
    Windows behavior is documented in the daemon RUNBOOK.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _build_sighup_handler(config: V4Config) -> Callable[..., None]:
    """Build the SIGHUP handler closure used by ``cmd_daemon_start``.

    Extracted to module level so tests can exercise the reset +
    reconfigure path directly without invoking ``cmd_daemon_start``
    (which would call ``daemon.run_forever()`` and block).

    The captured ``log_dir`` + ``log_level`` are the SAME shape the
    daemon used at start — SIGHUP reloads logging, not config.
    """
    log_dir = Path(config.log_dir)
    log_level = config.log_level

    def _reload_logging(*_args: object) -> None:
        reset_logging()
        configure_logging(log_dir=log_dir, level=log_level)

    return _reload_logging


def cmd_daemon_start(ctx: typer.Context) -> None:
    """Start the daemon in the foreground.

    Tests inject the prepared Daemon via build_cli_context(daemon=...).
    Production wiring (Phase 7) builds the Daemon from config and feeds
    it into build_cli_context the same way.

    SIGTERM + SIGINT trigger graceful shutdown. SIGHUP (on platforms
    that have it) resets + reconfigures logging so file handlers don't
    stack on repeated reloads.
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

        # SIGHUP (POSIX only) → reset + reconfigure logging. We use
        # the V4Config threaded through ctx.obj so the handler reloads
        # logging with the same log_dir + log_level that the daemon
        # started with. Tests / minimal wiring that don't supply a
        # config skip the install (no handler stacking to prevent
        # because there's no real logging surface).
        if _SIGHUP is not None and ctx.obj.config is not None:
            signal.signal(_SIGHUP, _build_sighup_handler(ctx.obj.config))

        daemon.run_forever()
    finally:
        if _PID_PATH.exists():
            _PID_PATH.unlink()


def cmd_daemon_stop(ctx: typer.Context) -> None:
    if not _PID_PATH.exists():
        typer.echo("no daemon PID file", err=True)
        raise typer.Exit(code=1)
    pid = int(_PID_PATH.read_text().strip())
    if not _is_pid_alive(pid):
        typer.echo(f"PID {pid} not running; cleaning stale file")
        _PID_PATH.unlink()
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Race: process died between the liveness check and the
        # SIGTERM. Same outcome as the up-front stale path.
        typer.echo(f"PID {pid} not running; cleaning stale file")
        _PID_PATH.unlink()
        return
    typer.echo(f"sent SIGTERM to {pid}")


def cmd_daemon_status(ctx: typer.Context) -> None:
    if not _PID_PATH.exists():
        typer.echo("daemon: not running")
        return
    pid = int(_PID_PATH.read_text().strip())
    if _is_pid_alive(pid):
        typer.echo(f"daemon: running (pid {pid})")
    else:
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
