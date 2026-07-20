"""bootstrap_cli_context — production startup wiring.

This is the single place where V4Config gets turned into the live
object graph. Tests use the factories to inject fakes; production
calls with the real PyGithub-backed factories.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from foreman.v4.cli.context import CliContext, build_cli_context
from foreman.v4.clone_refresh import CloneRefresher
from foreman.v4.config import ProjectConfig, V4Config
from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import GitProvider
from foreman.v4.identity import V4IdentityRegistry
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
from foreman.v4.sandbox import SandboxLauncher, SandboxUnavailableError, preflight
from foreman.v4.subprocess_dispatcher import BotMetadata, SubprocessRoleDispatcher
from foreman.worktree import ensure_clone

logger = logging.getLogger(__name__)


class IdentityProvider(Protocol):
    """Structural interface for minting a per-role GitHub token.

    ``bootstrap_cli_context`` depends on this rather than a concrete
    identity implementation, so production can pass the real
    App-installation-token registry while tests pass a fake.
    """

    def get_role_token(self, role: str) -> str:
        """Return a live GitHub token scoped to ``role``."""
        ...


def bootstrap_cli_context(
    *,
    config: V4Config,
    identity: IdentityProvider,
    git_provider_factory: Callable[[str], GitProvider],
    foreman_cli: list[str] | None = None,
    projects: list[ProjectConfig] | None = None,
    projects_loader: Callable[[], list[ProjectConfig]] | None = None,
    run_startup_clone: bool = True,
) -> CliContext:
    """Build the full v4 object graph from config.

    ``git_provider_factory`` takes a repo full name (``owner/name``) and
    returns a GitProvider for it. Production passes a function that
    constructs PyGithubGitProvider; tests pass a function that returns
    a FakeGitProvider.

    ``projects`` — when supplied, replaces ``config.projects`` as the
    boot-time project list.  Used by :func:`~foreman.v4.cli.main` after
    issue #477 so the daemon reads ``[[projects]]`` from the host-mounted
    ``projects.toml`` rather than from the envsubst-rendered config.

    ``projects_loader`` — a zero-arg callable stored on the Daemon for
    hot-reload on SIGHUP.  Production passes
    ``lambda: load_projects(projects_path)``; tests may omit it.

    ``run_startup_clone`` — when False, skip the daemon-level all-projects
    clone-maintenance loop (and the orchestrator-token mint it needs). Set
    False for a sandboxed role subprocess, whose private clone the daemon
    already prepped and which has no PEM to mint with.
    """
    configure_logging(log_dir=Path(config.log_dir), level=config.log_level)

    # issue #477: ``projects`` (from the host-mounted projects.toml) wins
    # over ``config.projects`` (always empty after the template change).
    # Resolved here — before the clone loop — so the loop iterates the
    # same list that downstream consumers (pollers, Daemon, etc.) use.
    active_projects = projects if projects is not None else config.projects

    # Startup auto-clone: ensure each project's local_clone_path exists and
    # is a valid git clone before any poll or clone-refresh runs.
    # Uses the orchestrator App installation token (read-only; daemon-level
    # operation, not tied to any role). Raises RuntimeError if a path exists
    # but is not a git repo — daemon refuses to start with an actionable
    # message. Idempotent: an existing valid clone is a no-op.
    # Transient failures (network, auth, disk) are caught and logged as
    # warnings so the daemon still starts; corrupt/non-git paths (RuntimeError)
    # remain fatal with their existing actionable message.
    if run_startup_clone and active_projects:
        orch_token = identity.get_role_token("orchestrator")
        for pc in active_projects:
            clone_path = Path(pc.local_clone_path)
            if (clone_path / ".git").exists():
                continue  # already a valid clone — skip, no log
            logger.info(
                "startup: cloning missing project repo",
                extra={
                    "project": pc.name,
                    "repo": pc.repo,
                    "clone_path": str(clone_path),
                },
            )
            try:
                ensure_clone(
                    repo_url=f"https://github.com/{pc.repo}.git",
                    clone_path=clone_path,
                    token=orch_token,
                )
            except subprocess.CalledProcessError as exc:
                # Transient failure (network, auth, disk-full, repo-not-found).
                # Log as warning and continue — the daemon must still start;
                # the clone will be retried next restart or via ensure_clone
                # in WorktreeManager when the first ticket runs.
                logger.warning(
                    "startup: transient clone failure — skipping project",
                    extra={
                        "project": pc.name,
                        "repo": pc.repo,
                        "clone_path": str(clone_path),
                        "returncode": exc.returncode,
                    },
                )
                continue
            # Scrub the embedded token from the cloned remote URL so it is
            # not persisted verbatim in .git/config (mirrors the discipline
            # used by push_branch to avoid token leakage in git config).
            # Guard: only run if the clone actually produced a .git directory
            # (ensure_clone may be stubbed in tests and produce no on-disk
            # artifact — calling git against a non-existent cwd would crash).
            if (clone_path / ".git").exists():
                try:
                    subprocess.run(
                        [
                            "git",
                            "remote",
                            "set-url",
                            "origin",
                            f"https://github.com/{pc.repo}.git",
                        ],
                        cwd=clone_path,
                        check=True,
                        capture_output=True,
                    )
                except subprocess.CalledProcessError:
                    # Best-effort: failure to scrub the URL is not fatal.
                    logger.warning(
                        "startup: could not scrub token from remote URL",
                        extra={"project": pc.name, "clone_path": str(clone_path)},
                    )
            logger.info(
                "startup: project repo cloned",
                extra={
                    "project": pc.name,
                    "repo": pc.repo,
                    "clone_path": str(clone_path),
                },
            )

    repo: TicketRepository
    from foreman.v4.postgres_repository import PostgresTicketRepository

    # StorageConfig is Postgres-only and its validator guarantees a dsn.
    assert config.storage.dsn is not None  # StorageConfig validator guarantees
    repo = PostgresTicketRepository.from_dsn(
        config.storage.dsn,
        pool_min=config.storage.pool_min,
        pool_max=config.storage.pool_max,
    )

    # foreman#job-sandbox-isolation Task 6: build the sandbox launcher and
    # run the startup preflight BEFORE constructing the dispatcher. Fail
    # closed by default — a host that can't create the bwrap sandbox
    # refuses to start rather than silently running role subprocesses
    # unwrapped. ``allow_unsandboxed`` is the deliberate local-dev escape
    # hatch: downgrade to a loud warning and continue with launcher=None.
    sandbox_launcher: SandboxLauncher | None = None
    sandbox_scratch_root: Path | None = None
    sandbox_projects: dict[str, ProjectConfig] | None = None
    # foreman#role-identity hardening: a role subprocess already running
    # inside a sandbox box (FOREMAN_SANDBOXED=1, set by SandboxLauncher
    # when it execs the box — see subprocess_dispatcher.py) must NOT run
    # this block. A boxed role never dispatches sub-roles, so it has no
    # use for a SandboxLauncher or bot_metadata — and running preflight()
    # here would be a nested bwrap-boot-inside-the-bwrap-box that, under
    # fail-closed config (allow_unsandboxed=false), crashes the box
    # before the role even runs. The in-box config still carries
    # sandbox.enabled=true (it's the same config the daemon loaded), so
    # the guard needs this second condition to tell "am I the daemon
    # deciding whether to sandbox roles" apart from "am I already one of
    # those sandboxed roles."
    in_sandbox_box = os.environ.get("FOREMAN_SANDBOXED") == "1"
    if config.sandbox.enabled and not in_sandbox_box:
        # foreman#556: the project map lets the dispatcher resolve each
        # project's base clone + repo to prep the private per-job clone.
        # Built unconditionally here — independent of whether preflight
        # below succeeds — so the allow_unsandboxed local-dev downgrade
        # (which leaves sandbox_launcher None) never leaves this None
        # while config.sandbox.enabled is True; re-enabling the sandbox
        # later needs no further bootstrap change.
        sandbox_projects = {pc.name: pc for pc in active_projects}
        try:
            preflight(bwrap_path=config.sandbox.bwrap_path)
        except SandboxUnavailableError:
            if not config.sandbox.allow_unsandboxed:
                logger.error(
                    "sandbox preflight failed and allow_unsandboxed is false; "
                    "refusing to start. Fix the host's unprivileged user "
                    "namespaces or set [sandbox].allow_unsandboxed for local dev."
                )
                raise
            logger.warning(
                "sandbox preflight FAILED but allow_unsandboxed=true: running "
                "role subprocesses UNSANDBOXED (local-dev escape hatch). Every "
                "dispatch is unprotected — do not use this in production."
            )
        else:
            # Claude Code's Bash tool creates a per-session dir under
            # ``$CLAUDE_CONFIG_DIR/session-env`` on every shell invocation. The
            # box RO-binds the whole Claude config dir, so that mkdir fails with
            # ``EROFS: read-only file system`` and the Bash tool breaks for any
            # role that runs a shell command (e.g. a fixer's ``git commit``).
            # Overlay a writable tmpfs at just that subpath — proven necessary
            # by the 2026-07-20 sandbox dogfood (a fixer that could edit but
            # not commit). Ephemeral is fine: session-env is per-run state.
            _claude_config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "/root/.claude-container")
            sandbox_launcher = SandboxLauncher(
                cache_dir=config.sandbox.cache_dir,
                bwrap_path=config.sandbox.bwrap_path,
                claude_writable_session_dir=f"{_claude_config_dir}/session-env",
            )
            sandbox_scratch_root = Path(config.sandbox.scratch_root)
            logger.info(
                "sandbox enabled: role subprocesses run in bwrap boxes",
                extra={
                    "cache_dir": config.sandbox.cache_dir,
                    "scratch_root": config.sandbox.scratch_root,
                },
            )

    # foreman#role-identity: the sandbox box has no PEM, so it can't mint
    # its own bot slug/logins the way an unsandboxed role can via the
    # PEM-backed registry. Build the static metadata here, daemon-side,
    # from that same registry, and thread it into the dispatcher so it
    # can inject it into the box's env. ``identity`` is typed to the
    # narrow ``IdentityProvider`` Protocol (one method); the richer
    # ``get_app_slug`` / ``get_role_bot_logins`` calls need the concrete
    # ``V4IdentityRegistry``, which is what production always passes —
    # narrow via isinstance rather than widening the Protocol. Only
    # built when the sandbox is actually enabled; an unsandboxed
    # dispatcher never needs it.
    bot_metadata: BotMetadata | None = None
    if sandbox_launcher is not None and isinstance(identity, V4IdentityRegistry):
        bot_metadata = BotMetadata(
            slug_by_role={
                r: identity.get_app_slug(r) for r in ("planner", "reviewer", "fixer", "worker")
            },
            bot_logins=frozenset(identity.get_role_bot_logins()),
        )

    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=foreman_cli or ["foreman"],
        identity=identity,
        log_dir=Path(config.log_dir),
        timeout_seconds=config.role_timeout_seconds,  # Phase 5 carryover
        # foreman#483: no-output watchdog — kill+re-dispatch a role that
        # streams nothing for this long, rather than burning the full
        # role_timeout_seconds on a silent hang.
        inactivity_timeout_seconds=config.role_inactivity_timeout_seconds,
        sandbox=sandbox_launcher,
        sandbox_scratch_root=sandbox_scratch_root,
        sandbox_projects=sandbox_projects,
        bot_metadata=bot_metadata,
    )

    pollers: list[Poller] = []
    per_project_providers: dict[str, GitProvider] = {}
    for project_config in active_projects:
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
    project_configs: dict[str, ProjectConfig] = {pc.name: pc for pc in active_projects}

    # foreman#407: per-poll clone refresher. Builds a ``name -> clone path``
    # map from the project configs and refreshes each clone's
    # ``origin/<default>`` ref at most once per ``clone_refresh_seconds``.
    # ``from_projects`` returns a no-op sentinel for zero-project configs.
    clone_refresher = CloneRefresher.from_projects(
        {pc.name: Path(pc.local_clone_path) for pc in active_projects},
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
        projects_loader=projects_loader,
        git_provider_factory=git_provider_factory,
        # foreman#556: same scratch root the dispatcher above got — a
        # terminal landing (WorkerPool- or MergeCoordinator-driven) then
        # reclaims the ticket's per-job sandbox scratch. Reuses the local
        # already computed for the dispatcher rather than recomputing from
        # config, and stays None whenever the sandbox is disabled or its
        # preflight failed (sandbox_launcher stayed None too).
        sandbox_scratch_root=sandbox_scratch_root,
    )

    return build_cli_context(
        repo=repo,
        qm=daemon.qm,
        daemon=daemon,
        git=git_for_cross_project,
        dispatcher=dispatcher,
        config=config,
    )
