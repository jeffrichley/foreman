"""Foreman daemon — poller + worker as concurrent async tasks.

Composes the queue, locks, poller, and worker iteration into a long-running
asyncio service. ``start()`` returns immediately; ``shutdown()`` cancels
the background tasks and waits for in-flight work to drain.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from foreman.config import Config
from foreman.locks import TicketLockManager
from foreman.logging_setup import configure_daemon_logging
from foreman.poller import poll_project
from foreman.queue import DaemonQueue
from foreman.storage import Storage
from foreman.worker import RoleDispatcher, run_one_iteration


class _HostLike(Protocol):
    def search_foreman_labeled_issues(self, repo: str): ...
    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None: ...
    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> None: ...


_IN_FLIGHT_LABELS = frozenset({"foreman:planning", "foreman:implementing"})
_log = logging.getLogger("foreman.daemon")


class Daemon:
    """The Foreman daemon process."""

    def __init__(
        self,
        *,
        config: Config,
        host: _HostLike,
        role_dispatcher: RoleDispatcher,
    ) -> None:
        self.config = config
        self.host = host
        self.role_dispatcher = role_dispatcher
        self.queue = DaemonQueue()
        self.locks = TicketLockManager()
        self.storage = Storage(config.daemon.sqlite_path)
        self._tasks: list[asyncio.Task[None]] = []
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Initialize storage, run reconciliation, then launch tasks."""
        configure_daemon_logging(
            log_path=self.config.daemon.log_path,
            level=self.config.daemon.log_level,
        )
        self.storage.init()
        self._reconcile_in_flight()
        logging.getLogger("foreman.daemon").info(
            "daemon started",
            extra={"projects": list(self.config.projects.keys())},
        )
        self._tasks.append(asyncio.create_task(self._poller_loop()))
        self._tasks.append(asyncio.create_task(self._worker_loop()))

    def _reconcile_in_flight(self) -> None:
        """Halt any tickets in in-flight states by adding foreman:failed.

        If a ticket's labels include foreman:planning or foreman:implementing,
        the daemon crashed mid-role-run (since the live daemon's locks would
        otherwise prevent us reaching here). We can't safely auto-retry
        because roles are not yet idempotent. Mark failed; operator decides
        whether to clear the label and resume.
        """
        for project in self.config.projects.values():
            try:
                issues = self.host.search_foreman_labeled_issues(project.repo)
            except Exception:
                continue
            for issue in issues:
                labels = set(issue.labels)
                if not (labels & _IN_FLIGHT_LABELS):
                    continue
                if "foreman:failed" in labels:
                    continue
                self.host.add_issue_label(project.repo, issue.number, "foreman:failed")
                self.host.post_issue_comment(
                    project.repo,
                    issue.number,
                    "Daemon crashed during a role run (label was in an in-flight "
                    "state at startup). The ticket has been halted with "
                    "`foreman:failed` for inspection. Remove the label to resume "
                    "from current state, or use `foreman replay` to re-run from "
                    "a prior state.",
                )
                _log.warning(
                    "reconciliation halted in-flight ticket",
                    extra={
                        "project": project.repo,
                        "issue": issue.number,
                        "labels": sorted(labels),
                    },
                )

    async def shutdown(self) -> None:
        """Signal shutdown, cancel tasks, wait for them to finish."""
        if self._shutdown_event.is_set():
            return
        _log.info("daemon shutdown requested")
        self._shutdown_event.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        _log.info("daemon shutdown complete")

    async def _poller_loop(self) -> None:
        first = True
        while not self._shutdown_event.is_set():
            if not first:
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self.config.daemon.poll_interval_seconds,
                    )
                    return
                except TimeoutError:
                    pass
            first = False

            for project_name, project in self.config.projects.items():
                try:
                    changed = poll_project(
                        project_name=project_name,
                        project=project,
                        host=self.host,
                        storage=self.storage,
                    )
                except Exception:
                    _log.exception(
                        "poll_project failed",
                        extra={"project": project_name},
                    )
                    continue
                for ticket in changed:
                    self.queue.enqueue(ticket)

    async def _worker_loop(self) -> None:
        while not self._shutdown_event.is_set():
            advanced = await run_one_iteration(
                queue=self.queue,
                locks=self.locks,
                dispatcher=self.role_dispatcher,
                storage=self.storage,
                projects=self.config.projects,
            )
            if not advanced:
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=1.0)
                    return
                except TimeoutError:
                    pass
