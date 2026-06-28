"""bootstrap_cli_context — production startup wiring.

This is the single place where V4Config gets turned into the live
object graph. Tests use the factories to inject fakes; production
calls with the real PyGithub-backed factories.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from foreman.v4.cli.context import CliContext, build_cli_context
from foreman.v4.clone_refresh import CloneRefresher
from foreman.v4.config import ProjectConfig, V4Config
from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import GitProvider
from foreman.v4.logging_config import configure_logging
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.observers.label_observability import LabelObservabilityObserver
from foreman.v4.observers.metrics import MetricsObserver
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.observers.sustained_blocked import SustainedBlockedObserver
from foreman.v4.observers.terminal_landing import TerminalLandingObserver
from foreman.v4.pg_backup import BackupScheduler
from foreman.v4.poller import Poller
from foreman.v4.repository import TicketRepository
from foreman.v4.routing_git_provider import RoutingGitProvider
from foreman.v4.subprocess_dispatcher import SubprocessRoleDispatcher

logger = logging.getLogger(__name__)


class IdentityProvider(Protocol):
    def get_role_token(self, role: str) -> str: ...


def bootstrap_cli_context(
    *,
    config: V4Config,
    identity: IdentityProvider,
    git_provider_factory: Callable[[str], GitProvider],
    foreman_cli: list[str] | None = None,
) -> CliContext:
    """Build the full v4 object graph from config.

    ``git_provider_factory`` takes a repo full name (``owner/name``) and
    returns a GitProvider for it. Production passes a function that
    constructs PyGithubGitProvider; tests pass a function that returns
    a FakeGitProvider.
    """
    configure_logging(log_dir=Path(config.log_dir), level=config.log_level)
    repo: TicketRepository
    from foreman.v4.postgres_repository import PostgresTicketRepository

    # StorageConfig is Postgres-only and its validator guarantees a dsn.
    assert config.storage.dsn is not None  # StorageConfig validator guarantees
    repo = PostgresTicketRepository.from_dsn(
        config.storage.dsn,
        pool_min=config.storage.pool_min,
        pool_max=config.storage.pool_max,
    )

    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=foreman_cli or ["foreman"],
        identity=identity,
        log_dir=Path(config.log_dir),
        timeout_seconds=config.role_timeout_seconds,  # Phase 5 carryover
    )

    pollers: list[Poller] = []
    per_project_providers: dict[str, GitProvider] = {}
    for project_config in config.projects:
        # One GitProvider per project. The factory takes ``owner/name`` and
        # returns a provider locked to that repo (the PyGithub impl is
        # construction-time-bound to its repo_full_name and ignores the
        # ``project=`` kwarg on every Protocol method — see F1 in the
        # Phase 8d adversarial review). The router below pieces them back
        # together for any cross-project consumer.
        git_for_project = git_provider_factory(project_config.repo)
        per_project_providers[project_config.name] = git_for_project
        pollers.append(
            Poller(
                # Pollers always operate on ONE project — they take their
                # own per-project provider directly. Routing through the
                # dispatcher would be an unnecessary hop.
                repo=repo,
                qm=None,
                git=git_for_project,
                project=project_config.name,
                trigger_label=project_config.trigger_label,
                clock=dt.datetime.now,
            )
        )

    # Cross-project consumers (Daemon's state machine, the label
    # observer) need a single GitProvider that respects the ``project=``
    # kwarg on every call. RoutingGitProvider wraps the per-project map
    # and dispatches; the PyGithub impl alone cannot — it's bound at
    # construction to one repo_full_name. This is the F1 fix from Phase
    # 8d.16: before the router, only the FIRST project's provider was
    # wired into the Daemon + observer, so every other project's writes
    # silently hit project[0]'s repo (404 best case, mis-label worst case).
    git_for_cross_project: GitProvider | None = (
        RoutingGitProvider(providers=per_project_providers) if per_project_providers else None
    )

    # EventBus + standard observer set. Wiring lives here (not in
    # Daemon) so the bus + observers are constructed exactly once at
    # boot, and the same EventBus instance is threaded into Daemon +
    # WorkerPool at construction. The four observers are the v4
    # production baseline; project-specific observers can be added
    # later by subscribing additional callables to ``bus`` before the
    # Daemon ticks.
    bus = EventBus()
    bus.subscribe(StructuredLogObserver())
    bus.subscribe(EventArchiveObserver(repo=repo))
    if git_for_cross_project is not None:
        # No projects ⇒ no GitProvider ⇒ nothing to write labels with.
        # The bus still gets the other three observers so the rest of
        # the surface is testable in zero-project configs.
        bus.subscribe(
            LabelObservabilityObserver(
                writer=git_for_cross_project,
                repo=repo,
            )
        )
    bus.subscribe(MetricsObserver())
    if git_for_cross_project is not None:
        # foreman#410: observers use the refresh-aware v4 GitProvider
        # (RoutingGitProvider) rather than the legacy per-project
        # GitHostProvider. The v4 provider's github_factory seam
        # rebuilds the PyGithub client past 3000s, so these observers
        # never 401 on a long-running daemon.
        bus.subscribe(
            SustainedBlockedObserver(
                repo=repo,
                git=git_for_cross_project,
            )
        )
        bus.subscribe(
            TerminalLandingObserver(
                repo=repo,
                git=git_for_cross_project,
                log_dir=Path(config.log_dir),
            )
        )

    # foreman#357: per-project ProjectConfig map keyed by name. Mirrors
    # the shape of ``per_project_providers`` above — config resolution
    # happens at startup, the state machine receives a flat dict it can
    # look up by ticket.project. MergingState reads
    # ``dev_base_branch`` from this map to gate the impl-PR merge on
    # base-ref match.
    project_configs: dict[str, ProjectConfig] = {pc.name: pc for pc in config.projects}

    # foreman#407: per-poll clone refresher. Builds a ``name -> clone path``
    # map from the project configs and refreshes each clone's
    # ``origin/<default>`` ref at most once per ``clone_refresh_seconds``.
    # ``from_projects`` returns a no-op sentinel for zero-project configs.
    clone_refresher = CloneRefresher.from_projects(
        {pc.name: Path(pc.local_clone_path) for pc in config.projects},
        interval_seconds=config.clone_refresh_seconds,
        clock=dt.datetime.now,
    )

    # foreman#434: pg_dump snapshot scheduler. The assert above guarantees
    # config.storage.dsn is non-None; pass it directly to from_config.
    backup_scheduler = BackupScheduler.from_config(
        config.backup,
        dsn=config.storage.dsn,
        bus=bus,
        clock=lambda: dt.datetime.now(dt.UTC),
    )

    daemon = Daemon(
        repo=repo,
        git=git_for_cross_project,  # type: ignore[arg-type]
        dispatcher=dispatcher,
        pollers=pollers,
        config=DaemonConfig(
            tick_seconds=config.tick_seconds,
            max_in_flight=config.max_in_flight,
            max_state_attempts=config.max_state_attempts,
        ),
        clock=dt.datetime.now,
        bus=bus,
        project_configs=project_configs,
        clone_refresher=clone_refresher,
        backup_scheduler=backup_scheduler,
    )

    return build_cli_context(
        repo=repo,
        qm=daemon.qm,
        daemon=daemon,
        git=git_for_cross_project,
        dispatcher=dispatcher,
        config=config,
    )
