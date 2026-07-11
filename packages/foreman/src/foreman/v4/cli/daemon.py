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

import logging
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from foreman.v4.logging_config import configure_logging, reset_logging

if TYPE_CHECKING:
    from foreman.v4.config import V4Config
    from foreman.v4.daemon import Daemon

logger = logging.getLogger(__name__)

# The PID file MUST live on an ephemeral, per-container-lifetime path — NOT a
# persistent volume/bind-mount. Across a container recreate (Watchtower redeploy)
# or a restart, PID *numbers* reset (fresh PID namespace), so a PID written by a
# prior daemon generation can collide with an unrelated live PID in the new
# container. That makes the single-instance start guard false-positive ("daemon
# already running (pid N)") and crash-loop the daemon — observed live 2026-06-26
# when a mid-run redeploy left a stale ``pid 7`` on the bind-mounted path.
# ``FOREMAN_PID_DIR`` points the container at a tmpfs (see docker-compose.yml)
# that is wiped on every container start; non-container dev runs fall back to the
# home dir so behavior there is unchanged.
_PID_DIR = Path(os.environ.get("FOREMAN_PID_DIR") or (Path.home() / ".foreman" / "v4"))
PID_PATH = _PID_DIR / "daemon.pid"

# Matches docker-compose's CLAUDE_CONFIG_DIR for the daemon container. The
# Claude Agent SDK + claude CLI store session transcripts under
# ``<CLAUDE_CONFIG_DIR>/projects/<munged-cwd>/<session-id>.jsonl`` — the
# substrate crash-recovery resume arm continues an interrupted role's
# session from there instead of re-running it from scratch.
_DEFAULT_CLAUDE_CONFIG_DIR = "/root/.claude-container"

# SIGHUP doesn't exist on Windows (signal module omits it). getattr
# (instead of `try: signal.SIGHUP`) keeps mypy happy on Windows hosts —
# the `try` form trips Module-has-no-attribute. Resolved once at import
# time so cmd_daemon_reload can fast-fail cleanly when reload is asked
# on a host that can't deliver it.
_SIGHUP: int | None = getattr(signal, "SIGHUP", None)


def is_pid_alive(pid: int) -> bool:
    """Probe whether ``pid`` corresponds to a live process.

    POSIX semantics: ``os.kill(pid, 0)`` succeeds for a live PID, raises
    ``ProcessLookupError`` for a dead PID, and may raise other OSError
    subtypes (e.g. EPERM) for a live PID we lack permission to signal —
    we let those bubble up rather than swallow them, because misreading
    them as "dead" would falsely report a running daemon as stale.

    Windows-native dev caveat: ``os.kill(pid, 0)`` is not a real Win32
    API — every PID raises ``OSError`` with ``winerror == 87``
    (``ERROR_INVALID_PARAMETER``), alive or dead. Production runs in
    Docker (POSIX), so this helper is correct where it matters. On a
    Windows host ``foreman daemon status``/``stop`` may therefore
    misreport a stale PID file — accepted as a non-issue because the
    daemon only ever runs as a POSIX process inside the container; it is
    never started as a Windows host process.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def warn_if_session_dir_ephemeral() -> bool:
    """Loudly warn (NOT fatal) if the Claude session dir isn't on a mount.

    Returns True when the session dir is persistent (on a mounted volume)
    OR when ``CLAUDE_CONFIG_DIR`` is unset (a non-container dev run, where
    resume persistence doesn't apply); False when the dir resolves onto the
    container's ephemeral writable layer.

    Crash-recovery's resume arm can only continue an interrupted role's
    Claude session if the transcript survives a container recreate (the
    Watchtower-redeploy case). Transcripts live under ``CLAUDE_CONFIG_DIR``;
    if that path is on the ephemeral writable layer rather than a mounted
    volume, every recreate silently wipes them and resume degrades to a
    fresh re-run. That degradation is SAFE — it is exactly the pre-resume
    Stage-1 behavior — so this is a loud warning, never a fatal error: we
    do not block daemon boot over an efficiency-only property. The fix is
    the ``foreman-claude-sessions`` volume in docker-compose.yml.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    if raw is None:
        # Not container-deployed (no override) → resume persistence is not
        # applicable; nothing to warn about.
        return True
    config_dir = Path(raw)
    if os.path.ismount(config_dir):
        return True
    logger.warning(
        "claude session dir %s is NOT a mounted volume — role session "
        "transcripts will be WIPED on container recreate (e.g. Watchtower "
        "redeploy), silently disabling crash-recovery resume. Mount a "
        "persistent volume there (docker-compose.yml: foreman-claude-"
        "sessions). Safe to run — degrades to fresh re-runs until fixed.",
        config_dir,
    )
    return False


def _build_sighup_handler(
    config: V4Config,
    daemon: Daemon | None = None,
) -> Callable[..., None]:
    """Build the SIGHUP handler closure used by ``cmd_daemon_start``.

    Extracted to module level so tests can exercise the reset +
    reconfigure path directly without invoking ``cmd_daemon_start``
    (which would call ``daemon.run_forever()`` and block).

    The captured ``log_dir`` + ``log_level`` are the SAME shape the
    daemon used at start — SIGHUP reloads logging configuration.

    When ``daemon`` is provided (production: always), the handler also
    calls ``daemon.request_project_reload()`` so the daemon's next
    ``tick_once()`` re-reads ``$FOREMAN_PROJECTS_PATH`` and diffs the
    result against the live project registry (issue #477).

    Backward-compatible: callers that don't pass ``daemon=`` (e.g.
    older tests) get the original logging-only handler.
    """
    log_dir = Path(config.log_dir)
    log_level = config.log_level

    def _reload_logging(*_args: object) -> None:
        reset_logging()
        configure_logging(log_dir=log_dir, level=log_level)
        if daemon is not None:
            daemon.request_project_reload()

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
    # Single-instance guard. run_forever's startup reconcile closes every
    # in-flight state_instances row as a crash orphan — correct ONLY if
    # exactly one daemon exists. A second daemon started while the first is
    # alive would close the live daemon's legitimately-in-flight row as a
    # crash orphan, corrupting an actively-running ticket. Refuse to start
    # if a live daemon already holds the PID file. A stale PID file (the
    # prior daemon crashed — the common Watchtower-redeploy case) is NOT a
    # live instance, so it falls through and is overwritten cleanly below.
    if PID_PATH.exists():
        existing_pid = int(PID_PATH.read_text().strip())
        if is_pid_alive(existing_pid):
            typer.echo(
                f"daemon already running (pid {existing_pid})",
                err=True,
            )
            raise typer.Exit(code=1)
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()))
    # Startup diagnostic for the crash-recovery resume arm: warn loudly (but
    # never fatally) if the Claude session dir isn't on a persistent mount,
    # so resumability can't be silently lost on a redeploy.
    warn_if_session_dir_ephemeral()
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_args: daemon.stop())

        # SIGHUP (POSIX only) → reset + reconfigure logging AND trigger a
        # project-list reload (issue #477).  We pass ``daemon=ctx.obj.daemon``
        # so the handler calls ``daemon.request_project_reload()`` after
        # resetting logging; the actual reload runs in the next ``tick_once()``
        # to avoid network I/O inside a signal context.
        if _SIGHUP is not None and ctx.obj.config is not None:
            signal.signal(
                _SIGHUP,
                _build_sighup_handler(ctx.obj.config, daemon=ctx.obj.daemon),
            )

        daemon.run_forever()
    finally:
        if PID_PATH.exists():
            PID_PATH.unlink()


def cmd_daemon_stop(ctx: typer.Context) -> None:
    """Send SIGTERM to the daemon recorded in the PID file.

    Triggers the same graceful-shutdown path ``run_forever`` installs a
    handler for. If the PID file is stale (process already dead), clean
    it up instead of signaling and report that to the operator.
    """
    if not PID_PATH.exists():
        typer.echo("no daemon PID file", err=True)
        raise typer.Exit(code=1)
    pid = int(PID_PATH.read_text().strip())
    if not is_pid_alive(pid):
        typer.echo(f"PID {pid} not running; cleaning stale file")
        PID_PATH.unlink()
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Race: process died between the liveness check and the
        # SIGTERM. Same outcome as the up-front stale path.
        typer.echo(f"PID {pid} not running; cleaning stale file")
        PID_PATH.unlink()
        return
    typer.echo(f"sent SIGTERM to {pid}")


def cmd_daemon_status(ctx: typer.Context) -> None:
    """Report whether the daemon PID file points at a live process.

    Reads the PID file (if present) and probes liveness with
    :func:`is_pid_alive`, distinguishing "not running" (no PID file),
    "running", and "stale PID file" (file present, process dead) so an
    operator knows whether a ``start`` would be refused or would clean
    up first.
    """
    if not PID_PATH.exists():
        typer.echo("daemon: not running")
        return
    pid = int(PID_PATH.read_text().strip())
    if is_pid_alive(pid):
        typer.echo(f"daemon: running (pid {pid})")
    else:
        typer.echo(f"daemon: stale PID file (pid {pid} not alive)")


def cmd_daemon_reload(ctx: typer.Context) -> None:
    """Send SIGHUP to the running daemon to reset + reconfigure logging.

    Exits 1 without signaling if the host has no SIGHUP (Windows) or if
    no PID file is present — reload only makes sense for a daemon that
    is actually tracked as running.
    """
    if _SIGHUP is None:
        typer.echo("reload not supported on this platform", err=True)
        raise typer.Exit(code=1)
    if not PID_PATH.exists():
        typer.echo("no daemon PID file", err=True)
        raise typer.Exit(code=1)
    pid = int(PID_PATH.read_text().strip())
    os.kill(pid, _SIGHUP)
    typer.echo(f"sent SIGHUP to {pid}")
