"""Foreman daemon — poller + worker as concurrent async tasks.

Composes the queue, locks, poller, and worker iteration into a long-running
asyncio service. ``start()`` returns immediately; ``shutdown()`` cancels
the background tasks and waits for in-flight work to drain.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from foreman.config import Config
from foreman.locks import TicketLockManager
from foreman.poller import poll_project
from foreman.queue import DaemonQueue
from foreman.storage import Storage
from foreman.worker import RoleDispatcher, run_one_iteration


class _HostLike(Protocol):
    def search_foreman_labeled_issues(self, repo: str): ...


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
        """Initialize storage, then launch poller and worker tasks."""
        self.storage.init()
        self._tasks.append(asyncio.create_task(self._poller_loop()))
        self._tasks.append(asyncio.create_task(self._worker_loop()))

    async def shutdown(self) -> None:
        """Signal shutdown, cancel tasks, wait for them to finish."""
        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

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
                except asyncio.TimeoutError:
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
                    await asyncio.wait_for(
                        self._shutdown_event.wait(), timeout=1.0
                    )
                    return
                except asyncio.TimeoutError:
                    pass
