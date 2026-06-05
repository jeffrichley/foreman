"""V3GitHubHost — adapter implementing the v3 ReconcilerHost Protocol.

REST methods (add_label / remove_label / post_comment / merge_pr) delegate
to v2's GitHubDaemonHost (already battle-tested). dispatch_role() spawns
`uv run foreman <subcommand>` as a subprocess via subprocess.Popen,
returns the PID immediately, and registers a background asyncio.Task that
awaits subprocess completion and writes the termination row to ExecutionLog.

The executor writes the 'running' start row first and passes its id into
``dispatch_role`` as ``start_log_id``; the host carries that id straight
through to the background tracker so the termination row is written even
when the bus is silent. This avoids the indirection that previously left
dispatch rows in ``outcome='running'`` until daemon restart.

The subprocess runner is injectable for testability. Tests that drive the
host synchronously (no running asyncio loop) should call
``terminate_dispatch(start_log_id=..., outcome=...)`` themselves; the early
return path keeps that case working without scheduling a background task.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Protocol

from foreman.reconciler.exec_log import ExecutionLog

logger = logging.getLogger(__name__)

# Map v3 role names to the CLI subcommand the v2 daemon exposes.
_ROLE_TO_SUBCOMMAND = {
    "planner": "plan",
    "reviewer": "review",
    "fixer": "fix",
    "worker": "implement",
}


class _V2HostLike(Protocol):
    """The v2 surface V3 needs. Matches foreman.daemon_host.GitHubDaemonHost."""

    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None: ...
    def remove_issue_label(self, repo: str, issue_number: int, label: str) -> None: ...
    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> None: ...
    def merge_pull_request(self, repo: str, pr_number: int) -> None: ...


class _SubprocessLike(Protocol):
    """The minimal subprocess surface V3 needs."""

    pid: int

    async def wait(self) -> int: ...


SubprocessRunner = Callable[..., _SubprocessLike]
"""Callable that spawns a subprocess and returns a ``_SubprocessLike`` wrapper.

Production runner accepts ``(argv, *, log_path: Path | None = None)``.
Test fakes may accept the same signature, but the ``log_path`` kwarg is
optional from the caller's side: ``dispatch_role`` only passes it when
``V3GitHubHost`` was constructed with ``log_dir`` set."""


class _PopenWrapper:
    """Adapter making subprocess.Popen match _SubprocessLike (pid + async wait()).

    Async wait() runs the blocking Popen.wait() on a thread executor so the
    daemon's running event loop isn't blocked.
    """

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self.pid = proc.pid
        # log_path is None for the no-capture path; populated by
        # _PopenWithLog when output is being captured. The tracker reads
        # this off the wrapper to surface the path in the execution log.
        self.log_path: Path | None = None

    async def wait(self) -> int:
        return await asyncio.to_thread(self._proc.wait)


class _PopenWithLog(_PopenWrapper):
    """``_PopenWrapper`` that owns the log file handle and closes it on
    ``wait()`` completion.

    foreman#119: dispatched role subprocesses used to redirect stdout +
    stderr to ``DEVNULL``, so diagnosing a returncode=N failure required
    replaying the dispatch manually outside the daemon. The wrapper now
    keeps the per-dispatch log file open for the subprocess's lifetime
    and closes the parent-side handle once the child exits (the kernel
    keeps the file alive until the child's handle closes too, so output
    that flushes during shutdown is still captured).
    """

    def __init__(
        self,
        proc: subprocess.Popen[bytes],
        log_fh: IO[bytes],
        log_path: Path,
    ) -> None:
        super().__init__(proc)
        self._log_fh = log_fh
        self.log_path = log_path

    async def wait(self) -> int:
        try:
            return await super().wait()
        finally:
            # Best-effort close; even if it raises we don't want to mask
            # a non-zero returncode the caller wants to act on.
            try:
                self._log_fh.close()
            except Exception:
                logger.warning(
                    "failed to close log file %s for pid=%d",
                    self.log_path,
                    self.pid,
                    exc_info=True,
                )


def _default_subprocess_runner(
    argv: list[str], *, log_path: Path | None = None
) -> _SubprocessLike:
    """Production runner. Spawn synchronously via subprocess.Popen.

    Using `asyncio.create_subprocess_exec` via `loop.run_until_complete` fails
    inside the daemon's tick (`RuntimeError: This event loop is already
    running`). subprocess.Popen is fully synchronous; we wrap it so callers
    can `await proc.wait()` without blocking the loop.

    When ``log_path`` is provided (foreman#119), the file is created (with
    parent dirs) and the subprocess's stdout + stderr are redirected to it,
    interleaved like a terminal (``stderr=subprocess.STDOUT``). The wrapper
    closes the parent-side file handle in ``wait()`` so we don't leak FDs.
    When ``log_path`` is ``None``, output is dropped to ``DEVNULL`` as
    before — preserves the existing behavior for code paths that haven't
    opted in.
    """
    if log_path is None:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return _PopenWrapper(proc)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("ab")
    try:
        proc = subprocess.Popen(
            argv,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
    except Exception:
        log_fh.close()
        raise
    return _PopenWithLog(proc, log_fh, log_path)


class V3GitHubHost:
    """v3 ReconcilerHost implementation."""

    def __init__(
        self,
        *,
        v2_host: _V2HostLike,
        log: ExecutionLog,
        subprocess_runner: SubprocessRunner | None = None,
        role_dispatch_timeout_seconds: int = 3600,
        max_concurrent_dispatches: int = 2,
        log_dir: Path | None = None,
    ) -> None:
        self._v2 = v2_host
        self._log = log
        self._runner = subprocess_runner if subprocess_runner is not None else _default_subprocess_runner
        self._timeout_seconds = role_dispatch_timeout_seconds
        # Global cap on concurrent dispatched role subprocesses. acquired() in
        # dispatch_role (non-blocking — raises when full), released in
        # _track_subprocess_completion's finally so the slot is held for the
        # entire subprocess lifecycle including timeout-termination.
        self._dispatch_capacity = threading.Semaphore(max_concurrent_dispatches)
        self._max_concurrent_dispatches = max_concurrent_dispatches
        # foreman#119: when ``log_dir`` is set, every dispatched role
        # subprocess gets its stdout + stderr captured to a per-dispatch
        # file under ``<log_dir>/<role>/<issue>__<iso-timestamp>.log``
        # and the path is recorded in the execution log so post-mortem
        # is just ``cat <path>``. When ``log_dir`` is ``None`` (test
        # default), output goes to DEVNULL as before — preserves the
        # existing behavior for any caller that hasn't opted in.
        self._log_dir = log_dir

    def add_label(self, *, owner: str, repo: str, issue: int, label: str) -> None:
        self._v2.add_issue_label(f"{owner}/{repo}", issue, label)

    def remove_label(self, *, owner: str, repo: str, issue: int, label: str) -> None:
        self._v2.remove_issue_label(f"{owner}/{repo}", issue, label)

    def post_comment(self, *, owner: str, repo: str, issue: int, body: str) -> None:
        self._v2.post_issue_comment(f"{owner}/{repo}", issue, body)

    def merge_pr(self, *, owner: str, repo: str, pr_number: int) -> None:
        self._v2.merge_pull_request(f"{owner}/{repo}", pr_number)

    def dispatch_role(
        self,
        *,
        role: str,
        target: str | None,
        owner: str,
        repo: str,
        issue: int,
        pr_number: int | None,
        start_log_id: int,
        project: str,
    ) -> int:
        """Spawn `uv run foreman <subcommand>` as a subprocess; return PID.

        ``target`` is ``"spec_pr"`` / ``"impl_pr"`` for Reviewer + Fixer (the
        target-ambiguous roles), and ``None`` for Planner + Worker. Plumbed
        into the subprocess via ``--target`` so the role CLI loads the right
        prompt and asserts the right entry label.

        ``start_log_id`` is the id of the 'running' row the executor wrote
        before calling this method. The background tracker carries it through
        and writes the termination row when the subprocess exits — so
        ``count_completed`` advances in production without depending on the
        worker's bus envelope landing.

        ``project`` is the project name the action is for. One V3GitHubHost
        instance is shared across all registered projects, so the project
        cannot be baked into ``__init__``; the executor passes
        ``ctx.snapshot.project`` per call. Plumbed into the subprocess via
        ``--project`` so the role CLI loads the right project config.
        """
        if not self._dispatch_capacity.acquire(blocking=False):
            raise RuntimeError(
                f"concurrency cap reached ({self._max_concurrent_dispatches} active "
                "dispatches); will retry next poll"
            )
        subcommand = _ROLE_TO_SUBCOMMAND.get(role)
        if subcommand is None:
            self._dispatch_capacity.release()
            raise ValueError(f"unknown role for dispatch: {role!r}")

        argv: list[str] = ["foreman", subcommand]

        if role == "reviewer":
            # `foreman review` takes a positional PR URL — no --issue-url flag.
            if pr_number is None:
                self._dispatch_capacity.release()
                raise ValueError("dispatch_role(role='reviewer') requires pr_number")
            pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
            argv.extend([pr_url, "--project", project])
            if target is not None:
                argv.extend(["--target", target])
        elif role in ("planner", "worker"):
            # `foreman plan` and `foreman implement` take positional ISSUE_URL.
            # They do not accept --pr-url; both roles OPEN the PR rather than
            # consume an existing one. Don't pass --target either — only the
            # target-ambiguous roles (reviewer, fixer) use it.
            issue_url = f"https://github.com/{owner}/{repo}/issues/{issue}"
            argv.extend([issue_url, "--project", project])
        elif role == "fixer":
            # `foreman fix` uses --issue-url + (optional) --pr-url + --target.
            issue_url = f"https://github.com/{owner}/{repo}/issues/{issue}"
            argv.extend(["--issue-url", issue_url, "--project", project])
            if pr_number is not None:
                pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
                argv.extend(["--pr-url", pr_url])
            if target is not None:
                argv.extend(["--target", target])
        else:
            # Defense-in-depth: the _ROLE_TO_SUBCOMMAND lookup above already
            # raises for unknown roles, but if a future role is added to the
            # map without a corresponding argv branch here, fail loudly rather
            # than spawn a malformed subprocess.
            self._dispatch_capacity.release()
            raise ValueError(f"unknown role for dispatch: {role!r}")

        # foreman#119: compute the per-dispatch log path before spawn so
        # the runner can open the file and wire stdout/stderr to it. When
        # log_dir was not configured (test default), log_path stays None
        # and the runner falls back to DEVNULL.
        log_path = self._build_dispatch_log_path(role=role, issue=issue)

        # Wrap runner + task creation in try/except so the capacity slot is
        # released on any spawn-time failure (uv missing → FileNotFoundError,
        # fork failure → OSError, loop scheduling failure, etc.). Without
        # this, repeated spawn failures would permanently consume slots until
        # the daemon restart triggers recover_orphaned + slot reset.
        try:
            if log_path is None:
                proc = self._runner(argv)
            else:
                proc = self._runner(argv, log_path=log_path)
        except Exception:
            self._dispatch_capacity.release()
            raise
        logger.info(
            "dispatched role=%s pid=%d log=%s argv=%s",
            role,
            proc.pid,
            log_path,
            argv,
        )

        # Background task: wait for subprocess, write termination row.
        # If there's no running event loop (e.g., synchronous unit tests), the
        # caller is responsible for invoking terminate_dispatch directly. We
        # still need to release the capacity slot in that case to avoid
        # leaking it.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._dispatch_capacity.release()
            return proc.pid

        try:
            loop.create_task(
                self._track_subprocess_completion(proc, role, start_log_id=start_log_id)
            )
        except Exception:
            self._dispatch_capacity.release()
            raise
        return proc.pid

    async def _track_subprocess_completion(
        self,
        proc: _SubprocessLike,
        role: str,
        *,
        start_log_id: int,
    ) -> None:
        """Await subprocess exit and write the termination row.

        The outer try/finally ensures the dispatch-capacity semaphore is
        released on every exit path (success, returncode!=0, timeout, error)
        so a stuck or crashed background task can never permanently consume
        a slot.
        """
        # foreman#119: surface the per-dispatch log path in every
        # termination row so post-mortem doesn't need to grep the daemon
        # log to find which file to read. None for the no-capture path.
        log_path = getattr(proc, "log_path", None)
        log_path_str = str(log_path) if log_path is not None else None
        try:
            try:
                returncode = await asyncio.wait_for(proc.wait(), timeout=self._timeout_seconds)
            except TimeoutError:
                logger.warning(
                    "subprocess for role=%s pid=%d timed out after %ds; terminating",
                    role,
                    proc.pid,
                    self._timeout_seconds,
                )
                # Attempt graceful termination via the wrapped Popen (best-effort).
                try:
                    inner = getattr(proc, "_proc", None)
                    if inner is not None and hasattr(inner, "terminate"):
                        inner.terminate()
                except Exception:
                    logger.exception("failed to terminate timed-out subprocess pid=%d", proc.pid)
                timeout_details: dict[str, Any] = {
                    "timeout_seconds": self._timeout_seconds,
                    "role": role,
                }
                if log_path_str is not None:
                    timeout_details["log_path"] = log_path_str
                self.terminate_dispatch(
                    start_log_id=start_log_id,
                    outcome="timeout",
                    details=timeout_details,
                )
                return
            except Exception as exc:
                logger.exception("subprocess for role=%s pid=%d errored awaiting", role, proc.pid)
                err_details: dict[str, Any] = {"error": str(exc)}
                if log_path_str is not None:
                    err_details["log_path"] = log_path_str
                self.terminate_dispatch(
                    start_log_id=start_log_id,
                    outcome="error",
                    details=err_details,
                )
                return

            outcome = "success" if returncode == 0 else "error"
            term_details: dict[str, Any] = {"returncode": returncode, "role": role}
            if log_path_str is not None:
                term_details["log_path"] = log_path_str
            self.terminate_dispatch(
                start_log_id=start_log_id,
                outcome=outcome,
                details=term_details,
            )
        finally:
            self._dispatch_capacity.release()

    def _build_dispatch_log_path(self, *, role: str, issue: int) -> Path | None:
        """Compute the per-dispatch log file path, or ``None`` when log
        capture is disabled (``log_dir`` not configured at host construction).

        foreman#119: file naming uses an ISO-8601 UTC timestamp with colons
        replaced by hyphens (Windows refuses ``:`` in path components):
        ``<log_dir>/<role>/<issue>__<YYYY-MM-DDTHH-MM-SS-microsecondsZ>.log``.
        The pid is recorded separately in the daemon log and the execution
        log's ``details``; including it in the filename would require a
        post-spawn rename that races with the subprocess.
        """
        if self._log_dir is None:
            return None
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S-%f")
        return self._log_dir / role / f"{issue}__{ts}Z.log"

    def terminate_dispatch(
        self,
        *,
        start_log_id: int,
        outcome: str,
        details: dict[str, Any],
    ) -> None:
        """Write the termination row for a dispatch_role start row.

        Called automatically by ``_track_subprocess_completion`` in production.
        Tests that drive the host without a running event loop call this
        directly to simulate subprocess exit.
        """
        self._log.terminate_action(
            parent_log_id=start_log_id, outcome=outcome, details=details
        )
