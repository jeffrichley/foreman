"""V3GitHubHost — adapter implementing the v3 ReconcilerHost Protocol.

REST methods (add_label / remove_label / post_comment / merge_pr) delegate
to v2's GitHubDaemonHost (already battle-tested). dispatch_role() spawns
`uv run foreman <subcommand>` as a subprocess via asyncio.create_subprocess_exec,
returns the PID immediately, and registers a background asyncio.Task that
awaits subprocess completion and writes the termination row to ExecutionLog.

The subprocess runner is injectable for testability.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from collections.abc import Callable
from typing import Any, Protocol

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


SubprocessRunner = Callable[[list[str]], _SubprocessLike]


class _PopenWrapper:
    """Adapter making subprocess.Popen match _SubprocessLike (pid + async wait()).

    Async wait() runs the blocking Popen.wait() on a thread executor so the
    daemon's running event loop isn't blocked.
    """

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self.pid = proc.pid

    async def wait(self) -> int:
        return await asyncio.to_thread(self._proc.wait)


def _default_subprocess_runner(argv: list[str]) -> _SubprocessLike:
    """Production runner. Spawn synchronously via subprocess.Popen.

    Using `asyncio.create_subprocess_exec` via `loop.run_until_complete` fails
    inside the daemon's tick (`RuntimeError: This event loop is already
    running`). subprocess.Popen is fully synchronous; we wrap it so callers
    can `await proc.wait()` without blocking the loop.
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return _PopenWrapper(proc)


class V3GitHubHost:
    """v3 ReconcilerHost implementation."""

    def __init__(
        self,
        *,
        v2_host: _V2HostLike,
        log: ExecutionLog,
        subprocess_runner: SubprocessRunner | None = None,
        project_name: str = "foreman",
        role_dispatch_timeout_seconds: int = 3600,
        max_concurrent_dispatches: int = 2,
    ) -> None:
        self._v2 = v2_host
        self._log = log
        self._runner = subprocess_runner if subprocess_runner is not None else _default_subprocess_runner
        self._project_name = project_name
        self._timeout_seconds = role_dispatch_timeout_seconds
        # Mapping pid -> parent_log_id. Caller (the reconciler or test fixture)
        # populates this before/after dispatch_role; background termination task
        # reads it on subprocess exit.
        self._pending_start_log_id_by_pid: dict[int, int] = {}
        # Global cap on concurrent dispatched role subprocesses. acquired() in
        # dispatch_role (non-blocking — raises when full), released in
        # _track_subprocess_completion's finally so the slot is held for the
        # entire subprocess lifecycle including timeout-termination.
        self._dispatch_capacity = threading.Semaphore(max_concurrent_dispatches)
        self._max_concurrent_dispatches = max_concurrent_dispatches

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
        owner: str,
        repo: str,
        issue: int,
        pr_number: int | None,
    ) -> int:
        """Spawn `uv run foreman <subcommand>` as a subprocess; return PID."""
        if not self._dispatch_capacity.acquire(blocking=False):
            raise RuntimeError(
                f"concurrency cap reached ({self._max_concurrent_dispatches} active "
                "dispatches); will retry next poll"
            )
        subcommand = _ROLE_TO_SUBCOMMAND.get(role)
        if subcommand is None:
            raise ValueError(f"unknown role for dispatch: {role!r}")

        argv: list[str] = ["uv", "run", "foreman", subcommand]

        if role == "reviewer":
            # `foreman review` takes a positional PR URL — no --issue-url flag.
            if pr_number is None:
                raise ValueError("dispatch_role(role='reviewer') requires pr_number")
            pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
            argv.extend([pr_url, "--project", self._project_name])
        else:
            issue_url = f"https://github.com/{owner}/{repo}/issues/{issue}"
            argv.extend(["--issue-url", issue_url, "--project", self._project_name])
            if pr_number is not None:
                pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
                argv.extend(["--pr-url", pr_url])

        proc = self._runner(argv)
        logger.info("dispatched role=%s pid=%d argv=%s", role, proc.pid, argv)

        # Background task: wait for subprocess, write termination row.
        # If no running event loop (e.g., unit tests passing _FakeProcess),
        # caller handles termination synthetically.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return proc.pid

        if loop.is_running():
            loop.create_task(self._track_subprocess_completion(proc, role))
        return proc.pid

    async def _track_subprocess_completion(self, proc: _SubprocessLike, role: str) -> None:
        """Await subprocess exit, look up its start_log_id, write termination row.

        The outer try/finally ensures the dispatch-capacity semaphore is
        released on every exit path (success, returncode!=0, timeout, error)
        so a stuck or crashed background task can never permanently consume
        a slot.
        """
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
                self._terminate_pending(
                    proc.pid,
                    outcome="timeout",
                    details={"timeout_seconds": self._timeout_seconds, "role": role},
                )
                return
            except Exception as exc:
                logger.exception("subprocess for role=%s pid=%d errored awaiting", role, proc.pid)
                self._terminate_pending(proc.pid, outcome="error", details={"error": str(exc)})
                return

            outcome = "success" if returncode == 0 else "error"
            self._terminate_pending(
                proc.pid,
                outcome=outcome,
                details={"returncode": returncode, "role": role},
            )
        finally:
            self._dispatch_capacity.release()

    def _terminate_pending(self, pid: int, *, outcome: str, details: dict[str, Any]) -> None:
        start_id = self._pending_start_log_id_by_pid.pop(pid, None)
        if start_id is None:
            logger.warning("no pending start_log_id for pid=%d; cannot terminate", pid)
            return
        self._log.terminate_action(parent_log_id=start_id, outcome=outcome, details=details)
