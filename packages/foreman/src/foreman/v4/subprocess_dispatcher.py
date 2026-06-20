"""SubprocessRoleDispatcher — production RoleDispatcher impl.

Shells out to ``foreman <subcmd>-v4 ...`` with the role's identity token
injected as GH_TOKEN. Returns the subprocess's stdout for the state
machine's verify hook to parse.

The mapping from v4 role names to CLI subcommands lives in
``_ROLE_TO_INVOCATION``. Adding a new role = one entry there.

Phase 8 strips the ``-v4`` suffix once the legacy CLI commands are
deleted; that's the only change required here at cutover.

foreman#368: subprocess output is also streamed to disk under
``<log_dir>/<role-base>/<ticket_id>__<iso>.log`` as it runs, so
operators can ``docker exec ... tail -f`` mid-run instead of staring at
nothing until the role exits. Two reader threads — one for stdout, one
for stderr — drain both pipes concurrently to avoid the single-thread
deadlock when one pipe buffer fills. Every line gets flushed
immediately so the on-disk log is mid-run readable. Stderr lines carry
a ``[stderr] `` prefix in the merged file. A start banner + exit-code
footer (or TIMEOUT / ABORTED marker) bracket the role's output.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

from foreman.v4.outcome import OUTCOME_MARKER

_STDERR_PREFIX = "[stderr] "


class IdentityProvider(Protocol):
    def get_role_token(self, role: str) -> str: ...


class RoleSubprocessError(RuntimeError):
    """Subprocess exited non-zero AND did not emit a FOREMAN_OUTCOME: line,
    OR the subprocess exceeded its timeout. Both failure modes carry the
    role + exit context in the message."""


@dataclass(frozen=True)
class _Invocation:
    subcommand: str
    target: str | None


_ROLE_TO_INVOCATION: dict[str, _Invocation] = {
    "planner":       _Invocation(subcommand="plan",      target=None),
    "reviewer-spec": _Invocation(subcommand="review",    target="spec"),
    "reviewer-impl": _Invocation(subcommand="review",    target="impl"),
    "fixer-spec":    _Invocation(subcommand="fix",       target="spec"),
    "fixer-impl":    _Invocation(subcommand="fix",       target="impl"),
    "worker":        _Invocation(subcommand="implement", target=None),
}


def _base_role(role: str) -> str:
    """Strip the ``-spec`` / ``-impl`` target suffix so target-aware roles
    land in their base-role log directory.

    ``reviewer-spec`` and ``reviewer-impl`` both → ``reviewer/`` so an
    operator looking for "what did Reviewer say about ticket 42?" doesn't
    have to know whether the role's last run was target=spec or
    target=impl. Matches the pre-v4 per-role log layout that's been
    sitting empty since the v4 substrate cutover (PR #333).
    """
    if role.endswith("-spec"):
        return role[: -len("-spec")]
    if role.endswith("-impl"):
        return role[: -len("-impl")]
    return role


def _fs_safe_iso_utc(now: dt.datetime) -> str:
    """ISO 8601 UTC with ``T`` / ``Z`` / ``:`` replaced for filesystem safety.

    Example: ``2026-06-19T23-30-12-456Z``. Colons aren't legal in NTFS
    filenames and ``T`` / ``Z`` (while legal) read better as separators
    when ``ls``-ing the directory.
    """
    # Ensure UTC. Caller is expected to pass a tz-aware UTC datetime; if
    # they pass naive, we treat as UTC explicitly (defensive, not lenient).
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    iso = now.astimezone(dt.UTC).isoformat(timespec="milliseconds")
    # isoformat -> "2026-06-19T23:30:12.456+00:00"
    # Normalize the trailing offset to a single 'Z' first so the global
    # ':' replace below doesn't mangle it.
    if iso.endswith("+00:00"):
        iso = iso[:-6] + "Z"
    return iso.replace("T", "-").replace(":", "-").replace(".", "-").replace("Z", "Z")


def _write_banner(
    log_file: IO[str],
    *,
    role: str,
    ticket_id: int,
    project: str,
    issue_number: int,
    started_at: dt.datetime,
    cmd: list[str],
) -> None:
    """Write the role's start banner to the open log file. Flushes."""
    log_file.write(
        "--- role subprocess start ---\n"
        f"role={role} ticket_id={ticket_id} "
        f"project={project} issue_number={issue_number}\n"
        f"started_at={started_at.isoformat()}\n"
        f"cmd={cmd}\n"
        "--- output ---\n"
    )
    log_file.flush()


def _stream_to_log(
    stream: IO[str],
    log_file: IO[str],
    log_lock: threading.Lock,
    *,
    prefix: str,
    capture: list[str] | None,
) -> None:
    """Reader-thread body: drain ``stream`` line-by-line into ``log_file``,
    flushing after every line so ``tail -f`` sees output mid-run.

    Two reader threads share one log file handle; the lock serializes
    writes so stdout + stderr lines don't interleave mid-line. Capture
    list (when provided) buffers stdout text for the state-machine
    contract — the return value of ``dispatch()`` must remain the
    subprocess's stdout, byte-for-byte (sans prefix).
    """
    try:
        for line in stream:
            with log_lock:
                log_file.write(f"{prefix}{line}")
                log_file.flush()
            if capture is not None:
                capture.append(line)
    finally:
        try:
            stream.close()
        except Exception:
            pass


class SubprocessRoleDispatcher:
    def __init__(
        self,
        *,
        foreman_cli: list[str],
        identity: IdentityProvider,
        log_dir: Path,
        timeout_seconds: int = 600,
    ) -> None:
        self._foreman_cli = foreman_cli
        self._identity = identity
        self._log_dir = log_dir
        self._timeout = timeout_seconds

    def dispatch(
        self, *, role: str, project: str, issue_number: int, ticket_id: int,
    ) -> str:
        try:
            inv = _ROLE_TO_INVOCATION[role]
        except KeyError as exc:
            raise ValueError(f"unknown role: {role}") from exc

        cmd = [
            *self._foreman_cli, inv.subcommand,
            "--project", project,
            "--issue-number", str(issue_number),
        ]
        if inv.target is not None:
            cmd += ["--target", inv.target]

        env = dict(os.environ)
        env["GH_TOKEN"] = self._identity.get_role_token(role)

        started_at = dt.datetime.now(dt.UTC)
        role_base = _base_role(role)
        role_log_dir = self._log_dir / role_base
        role_log_dir.mkdir(parents=True, exist_ok=True)
        log_path = (
            role_log_dir / f"{ticket_id}__{_fs_safe_iso_utc(started_at)}.log"
        )

        stdout_chunks: list[str] = []
        # ``buffering=1`` + explicit ``flush()`` after every line: belt
        # and suspenders. Python's text-mode line-buffering is honored on
        # POSIX but historically squishy on Windows; the explicit flushes
        # are the load-bearing guarantee.
        log_file = open(
            log_path, "w", encoding="utf-8", buffering=1, newline="",
        )
        try:
            _write_banner(
                log_file,
                role=role, ticket_id=ticket_id,
                project=project, issue_number=issue_number,
                started_at=started_at, cmd=cmd,
            )

            return self._run_and_stream(
                cmd=cmd, env=env, role=role,
                log_file=log_file, stdout_chunks=stdout_chunks,
            )
        except BaseException as exc:
            # Both RoleSubprocessError (TIMEOUT path) and any unexpected
            # exception land here. The TIMEOUT marker is already written
            # inside _run_and_stream before it raises; only mark ABORTED
            # for the non-timeout exception classes.
            if not isinstance(exc, RoleSubprocessError):
                try:
                    log_file.write("--- ABORTED ---\n")
                    log_file.flush()
                except Exception:
                    # Log file may already be in a broken state (e.g.,
                    # the exception happened mid-write); swallow so the
                    # original exception propagates uncorrupted.
                    pass
            raise
        finally:
            try:
                log_file.close()
            except Exception:
                pass

    def _run_and_stream(
        self,
        *,
        cmd: list[str],
        env: dict[str, str],
        role: str,
        log_file: IO[str],
        stdout_chunks: list[str],
    ) -> str:
        """Spawn the subprocess, drain stdout/stderr via two threads,
        wait for exit (or timeout), and return the captured stdout.

        Caller owns the log_file lifecycle; we just write to it.
        """
        # ``bufsize=1`` requests line-buffering on the child's pipes.
        # Combined with ``text=True`` (universal_newlines), reads land
        # one line at a time on most platforms, which keeps the
        # mid-run tail -f experience snappy. The reader threads still
        # iterate ``for line in stream`` which buffers on its own; the
        # net effect for sane CLI roles is per-line latency.
        proc = subprocess.Popen(
            cmd, env=env, text=True, bufsize=1,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None  # PIPE guarantees, but mypy needs it
        assert proc.stderr is not None

        log_lock = threading.Lock()
        t_out = threading.Thread(
            target=_stream_to_log,
            args=(proc.stdout, log_file, log_lock),
            kwargs={"prefix": "", "capture": stdout_chunks},
            daemon=True,
        )
        t_err = threading.Thread(
            target=_stream_to_log,
            args=(proc.stderr, log_file, log_lock),
            kwargs={"prefix": _STDERR_PREFIX, "capture": None},
            daemon=True,
        )
        t_out.start()
        t_err.start()

        try:
            returncode = proc.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            # Drain the threads before writing the marker so the marker
            # lands AFTER whatever the role had time to emit, not
            # interleaved with the final lines.
            t_out.join(timeout=5.0)
            t_err.join(timeout=5.0)
            with log_lock:
                log_file.write(f"--- TIMEOUT after {self._timeout}s ---\n")
                log_file.flush()
            # Stderr was streamed to disk but not captured in-memory;
            # operators get the full stderr from the log file. The
            # exception message just needs to carry the timeout fact.
            raise RoleSubprocessError(
                f"role={role} exceeded timeout {self._timeout}s; "
                f"see log file for partial output"
            ) from None

        t_out.join()
        t_err.join()
        with log_lock:
            log_file.write(f"--- exit code: {returncode} ---\n")
            log_file.flush()

        stdout = "".join(stdout_chunks)
        if returncode != 0 and OUTCOME_MARKER not in stdout:
            # Read the stderr back from the log file is overkill; the
            # log file has it on disk, and the message just needs to
            # carry enough to debug from. Match the pre-368 message
            # shape (operators / tests grep on "exited N").
            raise RoleSubprocessError(
                f"role={role} exited {returncode} without "
                f"emitting an outcome; see log file"
            )
        return stdout
