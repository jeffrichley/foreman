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
from typing import TYPE_CHECKING

from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import GitProvider
from foreman.v4.poller import Poller
from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import TicketRepository
from foreman.v4.role_dispatcher import RoleDispatcher
from foreman.v4.state_backup import (
    BackupSchedulerLike,
    _DisabledBackupScheduler,
)
from foreman.v4.worker_pool import WorkerPool

# Module-level singleton for the daemon's ``backup_scheduler`` default
# kwarg. Defined here (rather than inlined as ``= _DisabledBackupScheduler()``
# in the signature) so ruff's B008 lint doesn't flag the function-call-
# in-default pattern. The sentinel is stateless (``tick()`` returns None
# and writes no files) so sharing one instance across every
# ``Daemon(...)`` construction is safe — see ``state_backup._DisabledBackupScheduler``.
_DISABLED_BACKUP_SCHEDULER: BackupSchedulerLike = _DisabledBackupScheduler()

if TYPE_CHECKING:
    from foreman.v4.config import ProjectConfig


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """The handful of global knobs. Per-project fields would belong to a
    ProjectConfig, not here — keeping DaemonConfig small means the
    multi-project surface lives in the Pollers list, not in nested
    config.

    ``max_state_attempts`` defaults to 3 to match V4Config's default
    and to keep the existing test fixtures that construct DaemonConfig
    without the new kwarg green. Phase 8c.2 added this as runaway
    defense — the state machine refuses to re-enter the same state
    more than ``max_state_attempts`` consecutive times.
    """

    tick_seconds: float
    max_in_flight: int
    max_state_attempts: int = 3


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
        project_configs: dict[str, ProjectConfig] | None = None,
        backup_scheduler: BackupSchedulerLike = _DISABLED_BACKUP_SCHEDULER,
    ) -> None:
        self._repo = repo
        self._git = git
        self._dispatcher = dispatcher
        self._config = config
        self._clock = clock
        self._bus = bus
        # foreman#357: per-project config map forwarded to WorkerPool so
        # MergingState's base-ref guard can read dev_base_branch. Default
        # None → empty dict keeps test-only ``Daemon(...)`` constructions
        # working without the kwarg.
        self._project_configs: dict[str, ProjectConfig] = project_configs or {}
        self._qm = QueueManager(repo=repo, max_in_flight=config.max_in_flight)
        # Wire the shared QM into every Poller that was constructed without one.
        self._pollers = [self._with_qm(p) for p in pollers]
        self._pool = WorkerPool(
            repo=repo, qm=self._qm, dispatcher=dispatcher,
            git=git, bus=bus, clock=clock,
            max_state_attempts=config.max_state_attempts,
            project_configs=self._project_configs,
        )
        # Issue #360: in-process backup scheduler ticks alongside the
        # WorkerPool. Default is the stateless
        # ``_DisabledBackupScheduler`` sentinel — its ``tick()``
        # returns None and writes no files, so sharing one instance
        # across every test-only ``Daemon(...)`` construction is
        # safe. Production wires the real scheduler via
        # ``BackupScheduler.from_config(config.backup, ...)`` in
        # ``bootstrap_cli_context``.
        self._backup_scheduler = backup_scheduler
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
        # Issue #360: take a SQLite snapshot if the configured
        # interval has elapsed. The scheduler is
        # ``_DisabledBackupScheduler`` (no-op) when
        # ``config.backup.enabled = False`` OR when the daemon was
        # constructed without an explicit ``backup_scheduler=`` kwarg
        # (test path) — see ``BackupScheduler.from_config``. The
        # call is unconditional so the call site never drifts away
        # from the default-sentinel decision.
        self._backup_scheduler.tick()
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
            self.shutdown(wait=True)

    def stop(self) -> None:
        """Signal the loop to exit. Safe to call from a signal handler
        — threading.Event.set() is async-signal-safe."""
        self._stop.set()

    def shutdown(self, *, wait: bool = True) -> None:
        """Drain the WorkerPool. Idempotent — safe to call more than once.

        ``wait=True`` blocks until every in-flight transition completes;
        ``wait=False`` returns as soon as the executor stops accepting
        new submits. Delegates to ``WorkerPool.shutdown``, which wraps
        ``concurrent.futures.ThreadPoolExecutor.shutdown`` — itself
        documented as safe to call repeatedly.
        """
        self._pool.shutdown(wait=wait)
