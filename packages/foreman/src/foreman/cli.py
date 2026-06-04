"""Foreman CLI — `foreman plan` + `foreman review` + `foreman fix` +
`foreman implement` are the walking-skeleton entries.

`foreman init` is the onboarding entry — runs the one-shot setup pass
that prepares a new target repo + writes the project's config block.

Thickening will add: `foreman daemon ...`, `foreman project add`, etc.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

import click
from github import Auth, Github

from foreman.config import Config, load_config
from foreman.init import InitConfig, detect_matching_clone, run_init
from foreman.providers.anthropic_sdk import AnthropicSDKProvider
from foreman.roles.fixer import run_fixer
from foreman.roles.planner import run_planner
from foreman.roles.reviewer import run_reviewer
from foreman.roles.worker import run_worker
from foreman.storage import Storage


def _default_config_path() -> Path:
    return Path(os.environ.get("FOREMAN_CONFIG", str(Path.home() / ".foreman" / "config.toml")))


def _default_worktrees_root() -> Path:
    return Path(
        os.environ.get("FOREMAN_WORKTREES_ROOT", str(Path.home() / ".foreman" / "worktrees"))
    )


@click.group()
def cli() -> None:
    """foreman — multi-identity GitHub-issue-to-PR orchestrator."""


@cli.command()
@click.argument("issue_url", type=str)
@click.option("--project", required=True, help="Project name as defined in config.toml")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to foreman config (default: $FOREMAN_CONFIG or ~/.foreman/config.toml)",
)
def plan(issue_url: str, project: str, config_path: Path | None) -> None:
    """Run the Planner on a GitHub issue and open a spec PR."""
    cfg_path = config_path or _default_config_path()
    cfg = load_config(cfg_path)
    provider = AnthropicSDKProvider()
    result = asyncio.run(
        run_planner(
            issue_url=issue_url,
            config=cfg,
            project_name=project,
            worktrees_root=_default_worktrees_root(),
            provider=provider,
        )
    )
    pr = result.pr
    llm = result.llm_output
    click.echo(f"Planner complete — PR #{pr.number} at {pr.url}")
    click.echo(f"Branch: {pr.branch}")
    click.echo(f"Confidence: {llm.confidence}")
    click.echo(f"Summary: {llm.summary}")
    if llm.considered_alternatives:
        click.echo("Considered alternatives:")
        for alt in llm.considered_alternatives:
            click.echo(f"  - {alt}")


@cli.command()
@click.argument("pr_url", type=str)
@click.option("--project", required=True, help="Project name as defined in config.toml")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to foreman config (default: $FOREMAN_CONFIG or ~/.foreman/config.toml)",
)
def review(pr_url: str, project: str, config_path: Path | None) -> None:
    """Run the Reviewer on a spec PR opened by the Planner OR an impl
    PR opened by the Worker.

    The Reviewer derives spec-vs-impl from the PR's head branch shape
    (foreman/issue-<N> vs foreman/impl-<N>) — no flag required.
    """
    cfg_path = config_path or _default_config_path()
    cfg = load_config(cfg_path)
    provider = AnthropicSDKProvider()
    result = asyncio.run(
        run_reviewer(
            pr_url=pr_url,
            config=cfg,
            project_name=project,
            worktrees_root=_default_worktrees_root(),
            provider=provider,
        )
    )
    llm = result.llm_output
    click.echo(f"{llm.outcome}: {len(llm.findings)} findings, confidence={llm.confidence}")


@cli.command()
@click.argument("issue_url", type=str)
@click.option("--project", required=True, help="Project name as defined in config.toml")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to foreman config (default: $FOREMAN_CONFIG or ~/.foreman/config.toml)",
)
def fix(issue_url: str, project: str, config_path: Path | None) -> None:
    """Run the Fixer on an issue queued by the Reviewer.

    The issue must carry ``foreman:spec-fix``. The Fixer derives the spec
    PR from the issue's ``foreman/issue-<N>`` branch, applies addressable
    Reviewer findings to the spec doc, commits + pushes, and advances the
    label based on outcome.
    """
    cfg_path = config_path or _default_config_path()
    cfg = load_config(cfg_path)
    provider = AnthropicSDKProvider()
    result = asyncio.run(
        run_fixer(
            issue_url=issue_url,
            config=cfg,
            project_name=project,
            worktrees_root=_default_worktrees_root(),
            provider=provider,
        )
    )
    llm = result.llm_output
    addressed = len(llm.addressed_findings)
    unaddressed = len(llm.unaddressed_findings)
    click.echo(
        f"{llm.outcome}: {result.attempt}/3 attempt, {addressed} fixed, {unaddressed} unaddressed"
    )


@cli.command()
@click.argument("issue_url", type=str)
@click.option("--project", required=True, help="Project name as defined in config.toml")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to foreman config (default: $FOREMAN_CONFIG or ~/.foreman/config.toml)",
)
def implement(issue_url: str, project: str, config_path: Path | None) -> None:
    """Run the Worker on an issue queued by the Reviewer.

    The issue must carry ``foreman:spec-ready``. The Worker derives the
    spec PR from the issue's ``foreman/issue-<N>`` branch, implements
    the code per the spec doc on a stacked impl branch
    (``foreman/impl-<N>``), runs ``check_command`` to verify, commits +
    pushes, and opens the impl PR via Foreman core. The orchestrator
    re-runs ``check_command`` independently as ground truth and
    overrides the Worker's claim if they disagree.
    """
    cfg_path = config_path or _default_config_path()
    cfg = load_config(cfg_path)
    provider = AnthropicSDKProvider()
    result = asyncio.run(
        run_worker(
            issue_url=issue_url,
            config=cfg,
            project_name=project,
            worktrees_root=_default_worktrees_root(),
            provider=provider,
        )
    )
    llm = result.llm_output
    implemented = len(llm.implemented_sub_requests)
    skipped = len(llm.skipped_sub_requests)
    pr_part = result.pr_url if result.pr_url else "none"
    click.echo(
        f"{llm.outcome}: {result.attempt}/3 attempt, {implemented} implemented, "
        f"{skipped} skipped, did_check_pass={result.final_did_check_pass}, "
        f"PR={pr_part}"
    )


@cli.command()
@click.argument("repo")
@click.option(
    "--name",
    default=None,
    help=(
        "Project name used as the [projects.<name>] config key. "
        "Defaults to the <repo> portion of <owner/repo>."
    ),
)
@click.option(
    "--clone-path",
    "clone_path",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help=(
        "Local path to the repo's clone on disk. If omitted, the cwd is "
        "used when its 'origin' remote matches the target repo."
    ),
)
@click.option(
    "--check-command",
    "check_command",
    default="just check",
    show_default=True,
    help="Quality gate command Foreman's Worker runs before claiming done.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing [projects.<name>] block in the config.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Path to foreman config (default: $FOREMAN_CONFIG or ~/.foreman/config.toml)",
)
def init(
    repo: str,
    name: str | None,
    clone_path: Path | None,
    check_command: str,
    force: bool,
    config_path: Path | None,
) -> None:
    """Onboard a new GitHub repo onto Foreman.

    REPO is the target repo in ``owner/repo`` form.

    Writes the project's config block, creates Foreman's labels on the
    repo, writes a ``.foreman/INSTRUCTIONS.md`` template (skipping if it
    already exists), and best-effort verifies that each role's GitHub
    App is installed on the target repo.
    """
    cfg_path = config_path or _default_config_path()

    # Default --name from the repo's tail.
    if name is None:
        if "/" not in repo:
            raise click.ClickException("REPO must be in 'owner/repo' form to default --name.")
        name = repo.split("/", 1)[1]

    # Default --clone-path from cwd when cwd points at the same repo.
    if clone_path is None:
        detected = detect_matching_clone(Path.cwd(), repo)
        if detected is None:
            raise click.ClickException(
                "--clone-path is required (current directory's 'origin' "
                f"remote does not match {repo!r}). Re-run with "
                "--clone-path /path/to/clone."
            )
        clone_path = detected

    init_config = InitConfig(
        repo=repo,
        name=name,
        clone_path=clone_path,
        check_command=check_command,
        force=force,
        config_path=cfg_path,
    )

    # Admin client uses the operator's PAT for label creation. The
    # token env var name matches AdminConfig's default
    # (``FOREMAN_ADMIN_TOKEN``); operators can still set ``GH_TOKEN``
    # / ``GITHUB_TOKEN`` for parity with ``gh``.
    admin_token = (
        os.environ.get("FOREMAN_ADMIN_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    if not admin_token:
        raise click.ClickException(
            "No admin GitHub token found. Set FOREMAN_ADMIN_TOKEN (or "
            "GH_TOKEN / GITHUB_TOKEN) to a PAT with write access to "
            f"{repo!r} so foreman init can create labels."
        )
    admin_client = Github(auth=Auth.Token(admin_token))

    try:
        result = run_init(init_config, admin_client=admin_client)
    except (ValueError, FileExistsError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(result.summary)


# --- daemon lock-file helpers (foreman#72 + foreman#88) ---
#
# A single file at ``~/.foreman/daemon.lock`` serves two roles:
#  - It is the OS-level exclusive lock that prevents a second daemon
#    from starting (foreman#88).
#  - Its contents are the daemon's PID, written by ``DaemonLock`` on
#    acquisition, so ``daemon stop`` / ``daemon status`` can identify
#    the running daemon without a separate pid file.
#
# Keeping it to one file eliminates the "two stale-file lifecycles"
# class of bug that bit foreman#72's first revision on CI Windows.

_STOP_GRACE_SECONDS = 10.0
_STOP_POLL_INTERVAL_SECONDS = 0.1


def _resolve_lock_path(config: Config | None) -> Path:
    """Return the daemon's lock-file path.

    ``FOREMAN_LOCK_PATH`` env var wins over config, matching
    ``daemon_start``'s resolution order. When ``config`` is ``None``
    (e.g., ``daemon stop`` called on a host without a config file),
    falls back to the hardcoded default so stop / status still work.
    An empty-string env value is treated as unset.
    """
    env_override = os.environ.get("FOREMAN_LOCK_PATH") or None
    if env_override is not None:
        return Path(env_override).expanduser()
    if config is None:
        return Path("~/.foreman/daemon.lock").expanduser()
    return Path(config.daemon.lock_path).expanduser()


def _read_lock_file_pid(lock_path: Path) -> int | None:
    """Best-effort: parse the PID from a daemon lock file.

    Returns ``None`` when the file is missing, unreadable, or contains
    non-integer content (transient: daemon mid-write, or a corrupted
    file). Callers should treat ``None`` as "no addressable daemon".
    """
    try:
        text = lock_path.read_text(encoding="ascii").strip()
    except (FileNotFoundError, OSError):
        return None
    try:
        return int(text)
    except ValueError:
        return None


@cli.group()
def daemon() -> None:
    """Daemon lifecycle commands."""


@daemon.command("start")
@click.option(
    "--max-iterations",
    type=int,
    default=None,
    help="Stop after N worker iterations (testing only).",
)
def daemon_start(max_iterations: int | None) -> None:
    """Start the daemon in foreground."""
    from foreman.daemon_lock import DaemonLock, LockAcquisitionError

    config = _load_config_from_env()
    lock_path = _resolve_lock_path(config)
    try:
        with DaemonLock(lock_path):
            asyncio.run(_daemon_run(config=config, max_iterations=max_iterations))
    except LockAcquisitionError as exc:
        raise click.ClickException(str(exc)) from exc


@daemon.command("v3-start")
@click.option(
    "--dry-run/--execute",
    default=False,
    help="Dry-run mode: reconciler emits intended actions to the execution "
    "log with outcome='dry_run' but does NOT call the host. Use for first "
    "~6 polls post-cutover to gut-check the rule catalog before flipping "
    "to executing mode.",
)
@click.option(
    "--max-ticks",
    type=int,
    default=None,
    help="Run this many ticks then exit. Default: forever. 0 means wire "
    "everything and exit immediately (smoke-test the CLI path).",
)
def daemon_v3_start(dry_run: bool, max_ticks: int | None) -> None:
    """Start the v3 declarative reconciler daemon.

    GitHub IS the source of truth for ticket + PR state. The reconciler
    derives the right action per ticket from GH + execution log, then
    executes via the host. See docs/superpowers/specs/foreman-issue-106-spec.md.
    """
    from foreman.daemon_lock import DaemonLock, LockAcquisitionError
    from foreman.logging_setup import configure_daemon_logging
    from foreman.reconciler import ExecutionLog, Reconciler, ReconcilerProject

    # FOREMAN_CONFIG_PATH (v3) wins; falls back to FOREMAN_CONFIG (v2)
    # for parity with the v2 daemon's env-var convention.
    cfg_path = os.environ.get("FOREMAN_CONFIG_PATH") or os.environ.get("FOREMAN_CONFIG")
    if cfg_path is None:
        cfg_path = str(Path("~/.foreman/config.toml").expanduser())
    config = load_config(cfg_path)

    # v3 writes to its own log file so v2 vs v3 daemons can run side-by-
    # side during the cutover window without clobbering each other's
    # JSON-lines stream. Level reuses config.daemon.log_level — there's
    # no separate v3 knob yet; if one is needed, add ReconcilerConfig.log_level.
    v3_log_path = Path(os.path.expanduser("~/.foreman/v3-daemon.log"))
    configure_daemon_logging(
        log_path=v3_log_path,
        level=config.daemon.log_level,
    )

    lock_path = Path(os.path.expanduser(config.reconciler.lock_path))
    try:
        with DaemonLock(lock_path):
            db_path = Path(os.path.expanduser(config.reconciler.db_path))
            log = ExecutionLog(db_path)
            log.init()

            recovered = log.recover_orphaned()
            if recovered > 0:
                click.echo(
                    f"recovered {recovered} orphaned running row(s) from prior daemon"
                )

            # config.projects is dict[name, ProjectConfig]; ProjectConfig.repo
            # is "owner/name" form. Split into the ReconcilerProject shape.
            projects_list: list[ReconcilerProject] = []
            for proj_name, proj_cfg in config.projects.items():
                if "/" not in proj_cfg.repo:
                    raise click.ClickException(
                        f"project {proj_name!r} has malformed repo {proj_cfg.repo!r} "
                        "(expected 'owner/name')"
                    )
                owner, repo = proj_cfg.repo.split("/", 1)
                projects_list.append(
                    ReconcilerProject(name=proj_name, owner=owner, repo=repo)
                )
            projects = tuple(projects_list)

            if max_ticks == 0:
                # Smoke-test wiring without spinning the loop.
                click.echo(
                    f"v3-start wired: {len(projects)} projects, "
                    f"db={db_path}, dry_run={dry_run}"
                )
                return

            if not projects:
                click.echo("No projects configured; nothing to reconcile.")
                return
            gh, host = _build_v3_gh_and_host(config, log, projects[0].name)

            reconciler = Reconciler(
                projects=projects,
                log=log,
                gh=gh,
                host=host,
                dry_run=dry_run,
                alert_after_n_failures=config.reconciler.alert_after_n_failures,
                poll_interval_seconds=config.reconciler.poll_interval_seconds,
            )

            async def _shutdown_watcher(
                stop_event: asyncio.Event,
            ) -> None:
                await stop_event.wait()
                await reconciler.shutdown()

            async def _run() -> None:
                loop = asyncio.get_event_loop()
                stop_event = asyncio.Event()

                def _signal_handler() -> None:
                    stop_event.set()

                # add_signal_handler is POSIX-only; Windows raises
                # NotImplementedError. Fall through there — Ctrl-C still
                # raises KeyboardInterrupt which asyncio.run handles
                # natively, and `foreman daemon stop` (v2) signals via
                # process termination.
                try:
                    loop.add_signal_handler(signal.SIGTERM, _signal_handler)
                    loop.add_signal_handler(signal.SIGINT, _signal_handler)
                except (NotImplementedError, RuntimeError):
                    pass

                watcher = asyncio.create_task(_shutdown_watcher(stop_event))
                try:
                    if max_ticks is None:
                        await reconciler.run()
                    else:
                        for _ in range(max_ticks):
                            await reconciler.tick()
                            await asyncio.sleep(reconciler.poll_interval_seconds)
                finally:
                    watcher.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await watcher

            asyncio.run(_run())
    except LockAcquisitionError as exc:
        raise click.ClickException(str(exc)) from exc


def _build_v3_gh_and_host(
    config: Config, log: Any, project_name: str
) -> tuple[Any, Any]:
    """Construct the real GHGraphQLClient + ReconcilerHost for v3 runtime.

    Pulls the planner App's installation token from IdentityRegistry for the
    GraphQL observer (read-only; planner has GraphQL scope). Wraps v2's
    GitHubDaemonHost for REST + adds dispatch_role subprocess spawn.
    """
    from foreman.daemon_host import GitHubDaemonHost
    from foreman.identity import IdentityRegistry
    from foreman.reconciler.gh_graphql import HttpxGHGraphQLClient
    from foreman.reconciler.v3_host import V3GitHubHost

    project_config = config.projects[project_name]
    # IdentityRegistry needs orchestrator config — GitHubDaemonHost's REST
    # methods call get_orchestrator_client() on every call. Mirrors the v2
    # daemon construction path elsewhere in this file.
    registry = IdentityRegistry(project_config, orchestrator=config.orchestrator)
    v2_host = GitHubDaemonHost(identity_registry=registry)

    # Use the planner App's token for the GraphQL observer (read-only).
    planner_token = registry.get_token("planner")
    gh = HttpxGHGraphQLClient(token=planner_token)
    host = V3GitHubHost(v2_host=v2_host, log=log, project_name=project_name)
    return gh, host


@daemon.command("stop")
def daemon_stop() -> None:
    """Signal a running daemon to stop and wait for clean exit.

    Reads the daemon's PID from the lock file (``DaemonLock`` writes
    the PID on acquisition), sends SIGTERM, and polls the PID for
    process death up to ``_STOP_GRACE_SECONDS``. The lock file
    remains on disk after stop — its content is now stale but the
    OS lock is released when the daemon's fd closes, so the next
    ``daemon start`` succeeds and overwrites the PID.
    """
    try:
        config: Config | None = _load_config_from_env()
    except (FileNotFoundError, OSError):
        config = None
    lock_path = _resolve_lock_path(config)

    if not lock_path.exists():
        discover = (
            "tasklist | findstr foreman"
            if sys.platform == "win32"
            else "ps aux | grep foreman"
        )
        click.echo(
            f"No daemon lock file at {lock_path}. Either the daemon "
            f"was never started, or the lock file was removed. To find "
            f"a stray process: `{discover}`, then kill the PID directly."
        )
        return

    pid = _read_lock_file_pid(lock_path)
    if pid is None:
        click.echo(
            f"Lock file at {lock_path} has unreadable content. "
            f"Cannot identify the daemon PID; remove the file manually "
            f"if you're certain no daemon is running."
        )
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        click.echo(f"Pid {pid} not running (stale lock file).")
        return
    click.echo(f"Sent SIGTERM to daemon pid {pid}; waiting for graceful shutdown.")

    # Poll for process death via os.kill(pid, 0). On POSIX the daemon
    # catches SIGTERM and runs its cleanup before exiting; on Windows
    # os.kill(pid, SIGTERM) is TerminateProcess (hard kill) so the
    # process dies near-instantly. Either way, the OS releases the
    # lock when the daemon process dies — file content stays but the
    # exclusive lock is gone.
    deadline = time.monotonic() + _STOP_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            click.echo("Daemon stopped cleanly.")
            return
        time.sleep(_STOP_POLL_INTERVAL_SECONDS)
    click.echo(
        f"Daemon (pid {pid}) did not exit within {_STOP_GRACE_SECONDS}s. "
        f"It may still be running; investigate via the discovery "
        f"command before force-killing."
    )


@daemon.command("status")
def daemon_status() -> None:
    """Show daemon status — running / stopped."""
    try:
        config: Config | None = _load_config_from_env()
    except (FileNotFoundError, OSError):
        config = None
    lock_path = _resolve_lock_path(config)
    if not lock_path.exists():
        click.echo("Daemon: not running.")
        return
    pid = _read_lock_file_pid(lock_path)
    if pid is None:
        click.echo(f"Daemon: lock file at {lock_path} has unreadable content.")
        return
    try:
        os.kill(pid, 0)
        click.echo(f"Daemon: running (pid {pid}).")
    except (ProcessLookupError, OSError):
        click.echo(
            f"Daemon: stale lock file (pid {pid} dead). "
            f"The OS released the lock; the next `foreman daemon start` "
            f"will overwrite the file."
        )


def _load_config_from_env() -> Config:
    """Load config from FOREMAN_CONFIG env var or default ~/.foreman/config.toml."""
    path = os.environ.get("FOREMAN_CONFIG", str(Path("~/.foreman/config.toml").expanduser()))
    return load_config(path)


async def _daemon_run(*, config: Config, max_iterations: int | None) -> None:
    """Run the daemon foreground until SIGTERM or --max-iterations reached."""
    from foreman.daemon import Daemon
    from foreman.role_dispatch import RealRoleDispatcher

    host, runners = _resolve_host_and_runners(config)
    role_dispatcher = RealRoleDispatcher(config=config, runners=runners)

    daemon_instance = Daemon(config=config, host=host, role_dispatcher=role_dispatcher)
    await daemon_instance.start()

    if max_iterations is not None:
        # Test mode — drain queue up to N times, then shut down.
        for _ in range(max_iterations):
            await asyncio.sleep(0.1)
        await daemon_instance.shutdown()
        return

    # Production: wait until SIGTERM.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    except NotImplementedError:
        # Windows: asyncio loop does not support add_signal_handler.
        pass
    try:
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
    except NotImplementedError:
        pass
    await stop_event.wait()
    await daemon_instance.shutdown()


def _resolve_host_and_runners(config: Config) -> tuple[Any, Any]:
    """Build real GitHubDaemonHost + DaemonRunners from config.

    Reads the orchestrator bot's App ID + private key from the loaded
    config, mints an installation token, and constructs the host and
    runners adapters.

    Returns (None, None)-shaped stubs only if orchestrator config is
    absent (e.g., a fresh install without orchestrator-bot configured).
    The daemon will still start with these stubs but won't do useful
    work; the CLI prints a warning.
    """
    from pathlib import Path as _Path

    from foreman.daemon_host import GitHubDaemonHost
    from foreman.daemon_runners import DaemonRunners
    from foreman.identity import IdentityRegistry

    # If orchestrator config is missing, fall back to nulls + warn.
    # Fail fast on missing credentials here so operators see the
    # configuration error at startup rather than discovering it on the
    # first API call (the registry mints lazily).
    try:
        config.orchestrator.resolve_app_id()
        config.orchestrator.resolve_private_key_path()
    except RuntimeError as exc:
        click.echo(
            f"WARNING: orchestrator not configured ({exc}). "
            "Daemon will run but cannot reach GitHub. Configure "
            "[orchestrator] in ~/.foreman/config.toml to enable.",
            err=True,
        )
        return _build_null_host_and_runners()

    # Need a project for the registry. The orchestrator's installation
    # token is global to the App installation; the registry uses the
    # project's repo only as the installation-id lookup, so any
    # configured project works (use the first one to match the
    # historical "first repo" convention).
    if not config.projects:
        raise RuntimeError(
            "No projects configured; daemon needs at least one project "
            "to look up the orchestrator's installation token."
        )
    first_project = next(iter(config.projects.values()))

    registry = IdentityRegistry(
        first_project,
        orchestrator=config.orchestrator,
    )
    host = GitHubDaemonHost(identity_registry=registry)

    worktrees_root = _Path("~/.foreman/worktrees").expanduser()
    runners = DaemonRunners(host=host, worktrees_root=worktrees_root)

    return host, runners


def _build_null_host_and_runners() -> tuple[Any, Any]:
    """Stub host + runners for when orchestrator config is absent."""

    class _NullHost:
        def search_foreman_labeled_issues(self, repo: str) -> list[Any]:
            return []

        def add_issue_label(self, repo: str, issue_number: int, label: str) -> None:
            pass

        def post_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
            pass

    class _NullRunners:
        async def run_planner(self, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def run_reviewer(self, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def run_fixer(self, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def run_worker(self, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def merge_spec_pr(self, **kwargs: Any) -> Any:
            raise NotImplementedError

        async def merge_impl_pr(self, **kwargs: Any) -> Any:
            raise NotImplementedError

    return _NullHost(), _NullRunners()


@cli.command("ps")
def ps_cmd() -> None:
    """List active pipelines."""
    from foreman.ps import format_active_pipelines

    config = _load_config_from_env()
    storage = Storage(config.daemon.sqlite_path)
    storage.init()
    click.echo(format_active_pipelines(storage))


@cli.command("pipeline-detail")
@click.argument("project")
@click.argument("issue_number", type=int)
def pipeline_detail_cmd(project: str, issue_number: int) -> None:
    """Show detailed audit trail for one pipeline."""
    from foreman.ps import format_pipeline_detail

    config = _load_config_from_env()
    storage = Storage(config.daemon.sqlite_path)
    storage.init()
    click.echo(format_pipeline_detail(storage, project, issue_number))


@cli.group()
def worktree() -> None:
    """Worktree management."""


@worktree.command("clean")
@click.argument("project")
@click.argument("issue_number", type=int)
def worktree_clean(project: str, issue_number: int) -> None:
    """Delete the worktree for a project + issue."""
    root = os.environ.get(
        "FOREMAN_WORKTREES_ROOT",
        str(Path("~/.foreman/worktrees").expanduser()),
    )
    target = Path(root) / project / f"issue-{issue_number}"
    if not target.exists():
        click.echo(f"No worktree found at {target}.")
        return
    shutil.rmtree(target)
    click.echo(f"Removed {target}.")


def main() -> None:
    """Console-script entry point."""
    cli()


if __name__ == "__main__":
    main()
