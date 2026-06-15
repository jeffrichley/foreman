"""bootstrap_cli_context — production startup wiring.

This is the single place where V4Config gets turned into the live
object graph. Tests use the factories to inject fakes; production
calls with the real PyGithub-backed factories.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from foreman.v4.cli.context import CliContext, build_cli_context
from foreman.v4.config import V4Config
from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import GitProvider
from foreman.v4.logging_config import configure_logging
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.observers.label_observability import LabelObservabilityObserver
from foreman.v4.observers.metrics import MetricsObserver
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.poller import Poller
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.subprocess_dispatcher import SubprocessRoleDispatcher


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
    repo = SqliteTicketRepository.at_path(Path(config.db_path))

    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=foreman_cli or ["foreman"],
        identity=identity,
        timeout_seconds=config.role_timeout_seconds,  # Phase 5 carryover
    )

    pollers: list[Poller] = []
    git_for_pollers: GitProvider | None = None
    for project_config in config.projects:
        git_for_project = git_provider_factory(project_config.repo)
        if git_for_pollers is None:
            git_for_pollers = git_for_project
        pollers.append(Poller(
            repo=repo, qm=None, git=git_for_project,
            project=project_config.name,
            trigger_label=project_config.trigger_label,
            clock=dt.datetime.now,
        ))

    # EventBus + standard observer set. Wiring lives here (not in
    # Daemon) so the bus + observers are constructed exactly once at
    # boot, and the same EventBus instance is threaded into Daemon +
    # WorkerPool at construction. The four observers are the v4
    # production baseline; project-specific observers can be added
    # later by subscribing additional callables to ``bus`` before the
    # Daemon ticks.
    bus = EventBus()
    bus.subscribe(StructuredLogObserver())
    bus.subscribe(EventArchiveObserver(conn=repo.connection))
    if git_for_pollers is not None:
        # No projects ⇒ no GitProvider ⇒ nothing to write labels with.
        # The bus still gets the other three observers so the rest of
        # the surface is testable in zero-project configs.
        bus.subscribe(LabelObservabilityObserver(
            writer=git_for_pollers, repo=repo,
        ))
    bus.subscribe(MetricsObserver())

    daemon = Daemon(
        repo=repo,
        git=git_for_pollers,  # type: ignore[arg-type]
        dispatcher=dispatcher,
        pollers=pollers,
        config=DaemonConfig(
            tick_seconds=config.tick_seconds,
            max_in_flight=config.max_in_flight,
            max_state_attempts=config.max_state_attempts,
        ),
        clock=dt.datetime.now,
        bus=bus,
    )

    return build_cli_context(
        repo=repo,
        qm=daemon.qm,
        daemon=daemon,
        git=git_for_pollers,
        dispatcher=dispatcher,
        config=config,
    )
