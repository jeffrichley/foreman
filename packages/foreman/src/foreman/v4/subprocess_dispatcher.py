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

Resource-lifecycle invariant (post-review hardening): the dispatcher
guarantees on EVERY exit path — success, timeout, RoleSubprocessError,
unexpected exception, KeyboardInterrupt — that the subprocess is reaped
and the reader threads are joined before ``dispatch()`` returns or
raises. ``_run_and_stream`` owns this via a single ``try/finally``
around the post-Popen block; ``dispatch()`` owns the ``log_lock`` and
acquires it around any marker writes so reader-thread interleaving is
not possible. ``_stream_to_log`` wraps each write in its own try/except
so a write failure does NOT silently kill the reader thread (which
would deadlock the subprocess on a full pipe buffer); on failure it
records the failure and continues draining the pipe to /dev/null until
EOF.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, cast

from foreman.v4.outcome import OUTCOME_MARKER

logger = logging.getLogger(__name__)

_STDERR_PREFIX = "[stderr] "

# Generous reader-thread join timeout. The pipes have to fully drain
# before we return from dispatch; if the subprocess emitted a lot in
# its final moments the readers can lag the process exit by a beat.
# 30s is much longer than any realistic drain on a sane role but short
# enough that a stuck reader thread doesn't wedge a daemon worker
# forever — we log a warning and proceed.
_READER_JOIN_TIMEOUT_SECONDS = 30.0


class IdentityProvider(Protocol):
    """Narrow seam for fetching a role's current GitHub installation token."""

    def get_role_token(self, role: str) -> str:
        """Return the current token to use for API calls made as ``role``."""
        ...


class RoleSubprocessError(RuntimeError):
    """The role subprocess failed in a way ``dispatch()`` cannot recover from.

    Covers both failure modes: the subprocess exited non-zero without
    emitting a ``FOREMAN_OUTCOME:`` line, or it exceeded its timeout.
    Both carry the role + exit context + log path in the message so an
    operator can jump straight to the on-disk log.
    """


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
    """Strip the ``-spec`` / ``-impl`` target suffix from a role name.

    Target-aware roles land in their base-role log directory:
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
    return iso.replace("T", "-").replace(":", "-").replace(".", "-")


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
    writer_failed: threading.Event,
    *,
    prefix: str,
    capture: list[str] | None,
) -> None:
    """Reader-thread body: drain ``stream`` line-by-line into ``log_file``.

    Flushes after every line so ``tail -f`` sees output mid-run.
    Two reader threads share one log file handle; the lock serializes
    writes so stdout + stderr lines don't interleave mid-line. Capture
    list (when provided) buffers stdout text for the state-machine
    contract — the return value of ``dispatch()`` must remain the
    subprocess's stdout, byte-for-byte (sans prefix).

    Failure semantics: if ``log_file.write`` or ``log_file.flush`` raises
    (closed file, disk full, etc.), the reader does NOT die silently.
    Silent death would leave the corresponding pipe undrained, which
    deadlocks the subprocess on a full pipe buffer. Instead we record
    the failure via ``writer_failed`` + ``logger.exception`` and keep
    iterating the stream to discard remaining bytes until EOF, so the
    child can finish writing and exit cleanly.
    """
    write_broken = False
    try:
        for line in stream:
            if not write_broken:
                try:
                    with log_lock:
                        log_file.write(f"{prefix}{line}")
                        log_file.flush()
                except Exception:
                    # Surface this — silent reader-thread death is what
                    # deadlocks the subprocess. We keep draining the
                    # pipe (loop continues) so the child doesn't block,
                    # but we stop trying to write anything more.
                    write_broken = True
                    writer_failed.set()
                    logger.exception(
                        "role-stream writer failed; draining remainder "
                        "of stream to /dev/null to avoid pipe deadlock"
                    )
            if capture is not None and not write_broken:
                # Capture only the bytes we successfully recorded; once
                # writes are broken, capturing into memory would also
                # ship a corrupted return value to the state machine.
                capture.append(line)
    finally:
        try:
            stream.close()
        except Exception:
            # Stream may already be closed by the subprocess teardown;
            # not load-bearing.
            pass


class SubprocessRoleDispatcher:
    """Production ``RoleDispatcher``: runs each role as a ``foreman`` CLI subprocess.

    Maps a v4 role name to a ``foreman <subcmd>-v4 ...`` invocation via
    ``_ROLE_TO_INVOCATION``, injects the role's current identity token
    as ``GH_TOKEN``, streams stdout/stderr to a per-role log file on
    disk as the subprocess runs, and returns the captured stdout for
    the state machine's outcome-parsing verify hook.
    """

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
        state_instance_id: int | None = None,
        session_id: str | None = None,
        resume: bool = False,
    ) -> str:
        """Run ``role`` as a subprocess against ``project``/``issue_number`` and return its stdout.

        Builds the CLI invocation from ``_ROLE_TO_INVOCATION``, injects
        the role's current token as ``GH_TOKEN`` plus optional
        state-instance / session-resume env vars, streams both stdout
        and stderr to a per-role log file under ``log_dir`` as the
        process runs, and enforces ``timeout_seconds``. Raises
        :class:`RoleSubprocessError` on timeout, on a non-zero exit
        that never emitted a ``FOREMAN_OUTCOME:`` line, or on a
        mid-stream log-writer failure; raises ``ValueError`` for an
        unknown role.
        """
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
        # foreman#367: thread the current state-instance id into the
        # subprocess so the role-core dedup-key construction
        # (`state-instance-<id>`) is stable across retries on the same
        # state instance. When None (legacy / direct-CLI invocation),
        # the env var is OMITTED — the role-core fallback reads
        # ``os.environ.get("FOREMAN_STATE_INSTANCE_ID")`` as None and
        # uses the literal ``"unknown"`` for its dedup key.
        if state_instance_id is not None:
            env["FOREMAN_STATE_INSTANCE_ID"] = str(state_instance_id)
        # Crash-recovery resume arm: thread the Claude session id +
        # resume flag into the role subprocess. The role-core reads
        # FOREMAN_SESSION_ID (to pin) and FOREMAN_RESUME_SESSION_ID (to
        # resume that same session). Both omitted under normal operation,
        # so the role-core's reads return None and behavior is unchanged.
        if session_id is not None:
            env["FOREMAN_SESSION_ID"] = session_id
            # ``resume`` forwards the same session id; it is meaningless
            # (and there is no value to export) without one. Guarding on
            # ``session_id is not None`` also keeps the env value a str.
            if resume:
                env["FOREMAN_RESUME_SESSION_ID"] = session_id

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
        # Lock + failure-event are owned at the dispatch layer so we
        # can safely write the ABORTED/TIMEOUT markers without racing
        # the reader threads, AND so _run_and_stream can surface a
        # writer-failure result to the caller.
        log_lock = threading.Lock()
        writer_failed = threading.Event()
        try:
            _write_banner(
                log_file,
                role=role, ticket_id=ticket_id,
                project=project, issue_number=issue_number,
                started_at=started_at, cmd=cmd,
            )

            return self._run_and_stream(
                cmd=cmd, env=env, role=role,
                log_file=log_file, log_path=log_path,
                log_lock=log_lock, writer_failed=writer_failed,
                stdout_chunks=stdout_chunks,
            )
        except BaseException as exc:
            # Both RoleSubprocessError (TIMEOUT path) and any unexpected
            # exception land here. The TIMEOUT marker is already written
            # inside _run_and_stream before it raises; only mark ABORTED
            # for the non-timeout exception classes.
            if not isinstance(exc, RoleSubprocessError):
                try:
                    with log_lock:
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
        log_path: Path,
        log_lock: threading.Lock,
        writer_failed: threading.Event,
        stdout_chunks: list[str],
    ) -> str:
        """Spawn the subprocess and return its captured stdout once it exits.

        Drains stdout/stderr via two reader threads, waits for exit (or
        timeout). Caller owns the log_file lifecycle and the log_lock. All cleanup
        — kill+reap of the subprocess, join of reader threads — happens
        in the finally block so EVERY exit path (success, timeout,
        unexpected exception, BaseException like KeyboardInterrupt)
        leaves no orphaned process or thread behind.
        """
        # ``bufsize=1`` requests line-buffering on the child's pipes.
        # Combined with ``text=True`` (universal_newlines), reads land
        # one line at a time on most platforms, which keeps the
        # mid-run tail -f experience snappy. The reader threads still
        # iterate ``for line in stream`` which buffers on its own; the
        # net effect for sane CLI roles is per-line latency.
        #
        # encoding="utf-8" + errors="replace" pins the decode side of
        # ``text=True`` away from locale.getpreferredencoding() (which
        # is cp1252 on stock Windows). Without this the log file's
        # explicit utf-8 encoding mismatches what Popen hands us. The
        # errors="replace" is belt-and-suspenders for truly bad bytes
        # from a misbehaving child.
        proc = subprocess.Popen(
            cmd, env=env, text=True, bufsize=1,
            encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        # EVERYTHING from here through the finally clause must be inside
        # the try, including thread construction. If t_out / t_err
        # construction or .start() raises (synthetic test or real-world
        # OOM), the finally block must still reap the subprocess we
        # just spawned. A try/finally that excludes the spawn-and-wire
        # window leaks the subprocess on any failure between Popen()
        # and the wait loop.
        t_out: threading.Thread | None = None
        t_err: threading.Thread | None = None
        timed_out = False
        returncode: int | None = None
        try:
            # PIPE guarantees both streams are not-None; the cast keeps
            # mypy happy without an `assert` that python -O would strip.
            proc_stdout = cast(IO[str], proc.stdout)
            proc_stderr = cast(IO[str], proc.stderr)

            t_out = threading.Thread(
                target=_stream_to_log,
                args=(proc_stdout, log_file, log_lock, writer_failed),
                kwargs={"prefix": "", "capture": stdout_chunks},
                name=f"role-stream-stdout-{role}",
                daemon=True,
            )
            t_err = threading.Thread(
                target=_stream_to_log,
                args=(proc_stderr, log_file, log_lock, writer_failed),
                kwargs={"prefix": _STDERR_PREFIX, "capture": None},
                name=f"role-stream-stderr-{role}",
                daemon=True,
            )
            t_out.start()
            t_err.start()

            try:
                returncode = proc.wait(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                # proc.kill() + explicit wait happens in the finally
                # block below so the cleanup path is single-sourced.
                raise RoleSubprocessError(
                    f"role={role} exceeded timeout {self._timeout}s; "
                    f"see log at {log_path}"
                ) from None
        finally:
            # Resource cleanup runs on EVERY exit path: happy success,
            # TimeoutExpired-then-raise, RoleSubprocessError-from-below,
            # unexpected exceptions, KeyboardInterrupt. The invariant:
            # no orphaned subprocess, no live reader threads, when this
            # function returns or re-raises.
            if proc.poll() is None:
                # Subprocess still running. Kill + reap.
                try:
                    proc.kill()
                except Exception:
                    # Already dying or already dead; either way the
                    # wait() below handles the cleanup.
                    logger.exception(
                        "role=%s: proc.kill() failed during cleanup", role,
                    )
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    # Truly stuck. Log loud, move on — daemon worker
                    # mustn't wedge forever on one stuck child.
                    logger.warning(
                        "role=%s: subprocess did not exit within 5s "
                        "after kill; leaking PID %s", role, proc.pid,
                    )

            # Join the reader threads. They're draining pipes that the
            # OS may take a moment to flush after the child exits;
            # the bounded timeout protects against a stuck thread
            # holding up the daemon worker forever. Threads may be
            # None if Thread() construction itself raised before we
            # got to .start() — in that case there's nothing to join.
            for label, thread in (("stdout", t_out), ("stderr", t_err)):
                if thread is None:
                    continue
                thread.join(timeout=_READER_JOIN_TIMEOUT_SECONDS)
                if thread.is_alive():
                    logger.warning(
                        "role=%s: %s reader thread did not drain within "
                        "%.0fs; proceeding",
                        role, label, _READER_JOIN_TIMEOUT_SECONDS,
                    )

            # Write the TIMEOUT marker now that the readers have
            # finished, so the marker lands AFTER whatever the role
            # had time to emit (rather than interleaved). Happy-path
            # footer is written below this finally so it can include
            # the actual returncode.
            if timed_out:
                try:
                    with log_lock:
                        log_file.write(
                            f"--- TIMEOUT after {self._timeout}s ---\n"
                        )
                        log_file.flush()
                except Exception:
                    # If the log file is broken we can't do anything
                    # useful; just don't mask the original exception.
                    logger.exception(
                        "role=%s: failed to write TIMEOUT marker", role,
                    )

        # Past the finally: subprocess is reaped, readers are joined,
        # writer_failed reflects whether either reader hit an OSError
        # mid-stream. Happy path is the only way to reach here without
        # an in-flight exception.
        assert returncode is not None  # invariant on the happy path
        with log_lock:
            log_file.write(f"--- exit code: {returncode} ---\n")
            log_file.flush()

        if writer_failed.is_set():
            # The log file got a partial write history. The subprocess
            # may or may not have succeeded, but the operator needs to
            # know the on-disk record is incomplete and stdout in-memory
            # is truncated at the failure point.
            raise RoleSubprocessError(
                f"role={role} exited {returncode} but the log writer "
                f"failed mid-stream; see log at {log_path} for partial "
                f"output"
            )

        stdout = "".join(stdout_chunks)
        if returncode != 0 and OUTCOME_MARKER not in stdout:
            # Read the stderr back from the log file is overkill; the
            # log file has it on disk, and the message just needs to
            # carry enough to debug from. Operators get the full
            # stderr from the log file.
            raise RoleSubprocessError(
                f"role={role} exited {returncode} without "
                f"emitting an outcome; see log at {log_path}"
            )
        return stdout
