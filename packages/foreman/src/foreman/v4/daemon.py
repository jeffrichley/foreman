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
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from foreman.v4.clone_refresh import (
    CloneRefresherLike,
    _DisabledCloneRefresher,
)
from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import GitProvider
from foreman.v4.merge_coordinator import MergeCoordinator
from foreman.v4.pg_backup import (
    BackupSchedulerLike,
    _DisabledBackupScheduler,
)
from foreman.v4.poller import Poller
from foreman.v4.queue_manager import QueueManager
from foreman.v4.reconcile import reconcile_on_startup
from foreman.v4.repository import TicketRepository
from foreman.v4.role_dispatcher import RoleDispatcher
from foreman.v4.routing_git_provider import RoutingGitProvider
from foreman.v4.worker_pool import WorkerPool

# Module-level singletons for the daemon's default kwarg sentinels —
# avoids ruff B008 (function-call-in-default); the sentinels are stateless
# so sharing one instance across every ``Daemon(...)`` construction is safe.

# foreman#434: disabled backup scheduler sentinel. Used as the default
# ``backup_scheduler`` kwarg on ``Daemon.__init__`` so test-only ``Daemon(...)``
# constructions that don't pass ``backup_scheduler=`` stay no-ops.
_DISABLED_BACKUP_SCHEDULER: BackupSchedulerLike = _DisabledBackupScheduler()

# foreman#407: disabled clone refresher sentinel. Used as the default
# ``clone_refresher`` kwarg on ``Daemon.__init__``.
_DISABLED_CLONE_REFRESHER: CloneRefresherLike = _DisabledCloneRefresher()

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from foreman.v4.config import ProjectConfig, ProjectRegistry


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """The handful of global knobs shared by every project the daemon polls.

    Per-project fields would belong to a ProjectConfig, not here —
    keeping DaemonConfig small means the multi-project surface lives in
    the Pollers list, not in nested config.

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
    """Multi-project daemon owning the tick loop shared by every project's Poller.

    Holds a list of Pollers (one per project) sharing one QueueManager +
    one WorkerPool. Per-project Pollers can be constructed without a QM;
    the Daemon injects its shared QM into any Poller that arrived
    without one.
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
        clone_refresher: CloneRefresherLike = _DISABLED_CLONE_REFRESHER,
        backup_scheduler: BackupSchedulerLike = _DISABLED_BACKUP_SCHEDULER,
        projects_loader: Callable[[], list[ProjectConfig]] | None = None,
        git_provider_factory: Callable[[str], GitProvider] | None = None,
    ) -> None:
        from foreman.v4.config import ProjectRegistry

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
        #
        # issue #503 (FIX 1): wrap in ProjectRegistry so hot-reload swaps
        # the whole dict atomically (a single ``registry.current = new_dict``
        # reference rebind) instead of mutating the shared dict in place.
        # The WorkerPool holds the same registry object and reads
        # ``registry.current`` live at each access — no per-thread snapshot
        # is needed because the straddle-two-generations window is negligible.
        self._registry: ProjectRegistry = ProjectRegistry(project_configs or {})
        # issue #472: extract per-project caps and pass to QueueManager so it
        # can enforce per-project concurrency limits in its dequeue filter.
        # ProjectConfig.max_in_flight is pinned to 1 (per-repo serial), but
        # QueueManager stays general — it accepts int | None (None = unbounded)
        # so the annotation widens to match its signature.
        project_caps: dict[str, int | None] = {
            name: pc.max_in_flight for name, pc in self._registry.current.items()
        }
        self._qm = QueueManager(
            repo=repo,
            max_in_flight=config.max_in_flight,
            project_caps=project_caps,
        )
        # Wire the shared QM into every Poller that was constructed without one.
        self._pollers = [self._with_qm(p) for p in pollers]
        self._pool = WorkerPool(
            repo=repo,
            qm=self._qm,
            dispatcher=dispatcher,
            git=git,
            bus=bus,
            clock=clock,
            max_state_attempts=config.max_state_attempts,
            registry=self._registry,
        )
        # foreman#550: drains the merge_queue that Merging/SpecMerging enqueue
        # onto — nothing else does. ``projects`` reads ``self._registry.current``
        # live (not a snapshot) so a hot-reloaded project list is picked up
        # without reconstructing the coordinator, mirroring how the WorkerPool
        # holds the registry itself rather than a copy of its map.
        self._merge_coordinator = MergeCoordinator(
            repo=repo,
            git=git,
            projects=lambda: list(self._registry.current.keys()),
            clock=clock,
            qm=self._qm,
            bus=bus,
        )
        # foreman#407: per-poll project clone refresh. Default is the no-op
        # ``_DisabledCloneRefresher`` sentinel so test-only ``Daemon(...)``
        # constructions (and zero-project configs) stay no-ops; production
        # wires the real refresher in ``bootstrap_cli_context``.
        self._clone_refresher = clone_refresher
        # foreman#434: pg_dump snapshot scheduler. Default is the no-op
        # ``_DisabledBackupScheduler`` sentinel so test-only ``Daemon(...)``
        # constructions stay no-ops without specifying ``backup_scheduler=``.
        self._backup_scheduler = backup_scheduler
        self._stop = threading.Event()
        # issue #477: hot-reload of the project list on SIGHUP.
        # ``_reload_projects_event`` is set by ``request_project_reload()``
        # (called from the SIGHUP handler) and checked at the start of each
        # ``tick_once()`` call.  Doing the work in ``tick_once()`` (not in
        # the signal handler itself) avoids network I/O inside a signal context.
        self._reload_projects_event: threading.Event = threading.Event()
        self._projects_loader = projects_loader
        self._git_provider_factory = git_provider_factory
        # Narrowed type for the mutating register/unregister calls in
        # ``_apply_project_reload()``.  ``_git: GitProvider`` is the public
        # attribute (Protocol — no register_provider); ``_routing_git`` is
        # the concrete ``RoutingGitProvider | None`` used only inside
        # ``_apply_project_reload``.  When the daemon runs a plain stub
        # GitProvider (tests), ``_routing_git`` is None and provider
        # registration is silently skipped.
        self._routing_git: RoutingGitProvider | None = (
            git if isinstance(git, RoutingGitProvider) else None
        )

    @property
    def qm(self) -> QueueManager:
        """Exposed so the CLI can wire `ctx.obj.qm` to the shared QM."""
        return self._qm

    @property
    def _project_configs(self) -> dict[str, ProjectConfig]:
        """Read-only view of the current project-config map.

        Provided for backward-compatibility with tests that read
        ``daemon._project_configs`` directly.  Always returns
        ``_registry.current`` — the live, post-reload map.
        """
        return self._registry.current

    def request_project_reload(self) -> None:
        """Signal that ``_apply_project_reload`` should run on the next tick.

        Safe to call from a signal handler — :meth:`threading.Event.set` is
        async-signal-safe.  The actual reload runs in ``tick_once()`` (not in
        the signal handler) to avoid network I/O inside a signal context.
        """
        self._reload_projects_event.set()

    def _apply_project_reload(self) -> None:
        """Diff the project list from the loader against the live registry.

        Computes the added and removed sets from the current registry
        snapshot, then applies removals before additions so a changed
        project is treated as remove-then-re-add without self-overwrite
        confusion.  See the Approach section of the issue #477 spec for
        the ordering rationale.

        issue #503 FIX 1: after computing the full new project map, the
        registry is updated via a SINGLE atomic reference rebind
        (``self._registry.current = new_dict``) rather than in-place
        ``pop``/``insert`` mutations.  Worker threads read
        ``registry.current`` live at each access; the GIL makes the
        reference rebind atomic so they always see either the pre-reload
        or the post-reload snapshot — never a torn intermediate state.

        issue #503 FIX 3: if the loader raises ``FileNotFoundError`` or
        ``pydantic.ValidationError`` during a running-daemon reload, the
        exception is caught, a warning is logged, and the current project
        set is kept unchanged.  A malformed projects file edited during
        ``foreman daemon reload`` must never take the daemon down.

        No-ops when ``_projects_loader`` is ``None`` (daemon constructed
        without a loader — test isolation path).
        """
        import tomllib  # local import — only needed here
        from pathlib import Path  # local import — only needed here

        from pydantic import ValidationError

        if self._projects_loader is None:
            return

        # FIX 3: graceful degradation — bad file keeps the current set.
        try:
            new_projects = self._projects_loader()
        except (FileNotFoundError, ValidationError, tomllib.TOMLDecodeError) as exc:
            _log.warning(
                "config reload: failed to load projects file (%s: %s); "
                "keeping current project set unchanged",
                type(exc).__name__,
                exc,
            )
            return

        # Snapshot of the CURRENT registry for computing the diff.
        snapshot = self._registry.current
        new_by_name = {pc.name: pc for pc in new_projects}

        removed_names: set[str] = set()
        added_pcs: list[ProjectConfig] = []

        for name, old_pc in snapshot.items():
            if name not in new_by_name or new_by_name[name] != old_pc:
                removed_names.add(name)

        for name, new_pc in new_by_name.items():
            if name not in snapshot or snapshot[name] != new_pc:
                added_pcs.append(new_pc)

        if not removed_names and not added_pcs:
            _log.info("config reload: no changes")
            return

        # Build the new project map starting from the current snapshot.
        # We never mutate the existing dict — we build a fresh one and
        # swap the registry reference atomically at the end (FIX 1).
        working: dict[str, ProjectConfig] = dict(snapshot)

        # Step 6: process removals first (unregister providers / pollers).
        for name in removed_names:
            self._pollers = [p for p in self._pollers if p._project != name]
            if self._routing_git is not None:
                self._routing_git.unregister_provider(name)
            working.pop(name, None)
            self._clone_refresher.unregister_project(name)

        # Step 7: process additions (register providers / pollers).
        for pc in added_pcs:
            if self._git_provider_factory is not None:
                provider = self._git_provider_factory(pc.repo)
                if self._routing_git is not None:
                    self._routing_git.register_provider(pc.name, provider)
                new_poller = Poller(
                    repo=self._repo,
                    qm=None,
                    git=provider,
                    project=pc.name,
                    trigger_label=pc.trigger_label,
                    clock=self._clock,
                )
                self._pollers.append(self._with_qm(new_poller))
            working[pc.name] = pc
            self._clone_refresher.register_project(pc.name, Path(pc.local_clone_path))

        # FIX 1: single atomic reference rebind.  Worker threads reading
        # ``registry.current`` see either the old or the new snapshot;
        # never a half-mutated intermediate.
        self._registry.current = working

        _log.info(
            "config reload: added=%s removed=%s",
            sorted(pc.name for pc in added_pcs),
            sorted(removed_names),
        )

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
        # issue #477: apply a pending project-list reload before polling.
        # The event is set by ``request_project_reload()`` (called from the
        # SIGHUP handler).  Doing the work here (not in the signal handler)
        # avoids network I/O inside a signal context.  The check MUST be in
        # ``tick_once()`` so the reload tests — which call ``tick_once()``
        # directly — observe the effect immediately.
        if self._reload_projects_event.is_set():
            self._reload_projects_event.clear()
            self._apply_project_reload()
        # foreman#407: refresh each project's clone (throttled) BEFORE
        # polling, so any worktree created off the freshly-enqueued work
        # this tick branches from an up-to-date origin/<default> ref. The
        # refresher is interval-throttled internally and swallows
        # per-project failures, so this call is cheap on most ticks and
        # cannot crash the loop.
        self._clone_refresher.tick()
        # foreman I1 (self-heal arch-review, 2026-07-17): fault isolation.
        # A poller.tick() runs a synchronous GitHub sweep; an unhandled error
        # there (API blip, GithubException, a bug) must NOT propagate out of
        # the tick and kill run_forever — which, under Docker
        # ``restart: unless-stopped``, becomes a crash loop that takes down
        # EVERY project, not just the one that failed. Isolate per poller so
        # one project's failure can't starve the others, and log loudly (with
        # traceback) so the failure is still surfaced. Mirrors the way the
        # clone-refresher and backup-scheduler ticks already self-defend.
        for poller in self._pollers:
            try:
                poller.tick()
            except Exception:
                _log.exception(
                    "poller tick failed for project %r; isolating and continuing",
                    getattr(poller, "_project", "?"),
                )
        try:
            self._pool.tick()
        except Exception:
            _log.exception("worker-pool tick failed; continuing to next tick")
        # foreman#434: take a pg_dump snapshot if the interval has elapsed.
        # Placed after pool submission and before bounded-drain — symmetric
        # with the ordering of other side-effectful daemon work. The
        # scheduler swallows errors internally and is no-op when disabled.
        self._backup_scheduler.tick()
        # Bounded drain — see docstring.
        budget = 5.0
        while self._qm.in_flight_count() > 0 and budget > 0:
            time.sleep(0.01)
            budget -= 0.01
        # foreman#550/I1: drain the merge_queue AFTER the bounded drain so a
        # Merging/SpecMerging transition that just completed this tick (and
        # enqueued its PR) is visible to the coordinator immediately, rather
        # than waiting a full extra tick. Isolated in its own try/except —
        # mirrors the poller/pool boundaries above — so a coordinator error
        # (a GitHub API blip, a repo hiccup) cannot kill the loop.
        try:
            self._merge_coordinator.tick()
        except Exception:
            _log.exception("merge-coordinator tick failed; continuing to next tick")

    def _reconcile_startup(self) -> None:
        """Close crash-orphaned in-flight rows left by a previous process.

        Runs once, before the first tick, before any worker thread starts.
        Deliberately NOT in ``bootstrap_cli_context`` — that is built by every
        CLI command (``foreman ps``, ``foreman show``); reconciliation must
        fire only when the daemon actually starts processing.
        """
        reconcile_on_startup(self._repo, clock=self._clock)

    def run_forever(self) -> None:
        """Main loop. Returns when ``stop()`` is called."""
        self._reconcile_startup()
        try:
            while not self._stop.is_set():
                # foreman I1: outer fault boundary. Even with the per-component
                # guards in tick_once, run_forever must NEVER die from a tick
                # exception — a dead loop cannot self-heal, and Docker restart
                # turns a deterministic tick error into a crash loop. Log +
                # continue; the next tick retries. ``except Exception`` does not
                # catch KeyboardInterrupt / SystemExit (BaseException), so the
                # stop() signal path still exits cleanly.
                try:
                    self.tick_once()
                except Exception:
                    _log.exception("daemon tick failed; continuing to next tick")
                self._stop.wait(self._config.tick_seconds)
        finally:
            self.shutdown(wait=True)

    def stop(self) -> None:
        """Signal the loop to exit at the next tick boundary.

        Safe to call from a signal handler — threading.Event.set() is
        async-signal-safe.
        """
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
