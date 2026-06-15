"""Daemon — owns the Poller(s) + QueueManager + WorkerPool tick loop.

Multi-project from the start: Daemon takes a list of Pollers (one per
project) sharing one QM + one WorkerPool. Per-project concurrency caps
are enforced at the QM (max_in_flight is global). Phase 7 will only
add bootstrap wiring on top — no refactor.

Single-thread loop: every ``tick_seconds`` we poll every project then
drain the pool. Stop mechanic is a threading.Event; SIGTERM/SIGINT
installation lives in the CLI start command, not here.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import GitProvider
from foreman.v4.poller import Poller
from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import TicketRepository
from foreman.v4.role_dispatcher import RoleDispatcher
from foreman.v4.worker_pool import WorkerPool


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """Just the two global knobs. Per-project fields would belong to a
    ProjectConfig, not here — keeping DaemonConfig small means the
    multi-project surface lives in the Pollers list, not in nested
    config.
    """

    tick_seconds: float
    max_in_flight: int


class Daemon:
    """Multi-project daemon. Holds a list of Pollers (one per project)
    sharing one QueueManager + one WorkerPool. Per-project Pollers can
    be constructed without a QM; the Daemon injects its shared QM into
    any Poller that arrived without one.
    """

    def __init__(
        self,
        *,
        repo: TicketRepository,
        git: GitProvider,
        dispatcher: RoleDispatcher,
        pollers: list[Poller],
        config: DaemonConfig,
        clock: Callable[[], dt.datetime],
        bus: EventBus | None = None,
    ) -> None:
        self._repo = repo
        self._git = git
        self._dispatcher = dispatcher
        self._config = config
        self._clock = clock
        self._bus = bus
        self._qm = QueueManager(repo=repo, max_in_flight=config.max_in_flight)
        # Wire the shared QM into every Poller that was constructed without one.
        self._pollers = [self._with_qm(p) for p in pollers]
        self._pool = WorkerPool(
            repo=repo, qm=self._qm, dispatcher=dispatcher,
            git=git, bus=bus, clock=clock,
        )
        self._stop = threading.Event()

    @property
    def qm(self) -> QueueManager:
        """Exposed so the CLI can wire `ctx.obj.qm` to the shared QM."""
        return self._qm

    def _with_qm(self, poller: Poller) -> Poller:
        # Pollers can be constructed without a QM at config time; inject
        # the daemon's shared QM here so all pollers share one queue.
        if poller._qm is None:
            poller._qm = self._qm
        return poller

    def tick_once(self) -> None:
        """Poll every project, submit dequeued WorkItems, drain the pool.

        The WorkerPool's ``tick()`` is non-blocking — it submits to a
        ThreadPoolExecutor and returns. For tick_once to be useful in
        tests (assert state advanced), we wait for in-flight to clear
        before returning. The drain is bounded (5s) so a stuck future
        cannot hang the test suite forever.

        In ``run_forever`` the subsequent ``tick_seconds`` sleep would
        give the pool time to drain naturally; the explicit drain just
        makes tick_once deterministic.
        """
        for poller in self._pollers:
            poller.tick()
        self._pool.tick()
        # Bounded drain — see docstring.
        budget = 5.0
        while self._qm.in_flight_count() > 0 and budget > 0:
            time.sleep(0.01)
            budget -= 0.01

    def run_forever(self) -> None:
        """Main loop. Returns when ``stop()`` is called."""
        try:
            while not self._stop.is_set():
                self.tick_once()
                self._stop.wait(self._config.tick_seconds)
        finally:
            self._pool.shutdown(wait=True)

    def stop(self) -> None:
        """Signal the loop to exit. Safe to call from a signal handler
        — threading.Event.set() is async-signal-safe."""
        self._stop.set()
