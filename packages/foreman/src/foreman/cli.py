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
    # Delegates to foreman.config.resolve_config_path which honors
    # FOREMAN_CONFIG_PATH (container compose) AND legacy FOREMAN_CONFIG.
    from foreman.config import resolve_config_path
    return resolve_config_path()


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
    "--target",
    type=click.Choice(["spec_pr", "impl_pr"]),
    default=None,
    help=(
        "PR shape this Reviewer dispatch is targeting (spec_pr | impl_pr). "
        "Optional — when omitted, the Reviewer infers target from the PR "
        "head branch (foreman/issue-<N> vs foreman/impl-<N>). v3 dispatches "
        "pass --target explicitly for symmetry with the Fixer."
    ),
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to foreman config (default: $FOREMAN_CONFIG or ~/.foreman/config.toml)",
)
def review(
    pr_url: str,
    project: str,
    target: str | None,
    config_path: Path | None,
) -> None:
    """Run the Reviewer on a spec PR opened by the Planner OR an impl
    PR opened by the Worker.

    The Reviewer derives spec-vs-impl from the PR's head branch shape
    (foreman/issue-<N> vs foreman/impl-<N>); ``--target`` is accepted but
    currently advisory (the role infers target from the PR itself).
    """
    cfg_path = config_path or _default_config_path()
    cfg = load_config(cfg_path)
    provider = AnthropicSDKProvider()
    # ``target`` is accepted for symmetry with ``foreman fix`` and to keep
    # the v3 dispatch argv shape uniform; ``run_reviewer`` itself does not
    # take a ``target`` kwarg today (the Reviewer parses the PR head to
    # decide spec vs impl). Forwarding the flag would require a role-side
    # signature change — out of scope for the Stage-2 action split.
    _ = target
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
@click.option(
    "--issue-url",
    "issue_url",
    required=True,
    help="Full GitHub issue URL (https://github.com/owner/repo/issues/N).",
)
@click.option(
    "--pr-url",
    "pr_url",
    default=None,
    help=(
        "Full GitHub PR URL. Optional — the Fixer derives the spec PR from "
        "the issue's foreman/issue-<N> branch when omitted. v3's reconciler "
        "passes the PR URL explicitly for the impl-fix target so the "
        "dispatch is self-describing in logs."
    ),
)
@click.option("--project", required=True, help="Project name as defined in config.toml")
@click.option(
    "--target",
    type=click.Choice(["spec_pr", "impl_pr"]),
    default="spec_pr",
    show_default=True,
    help=(
        "Which Fixer flow to run. ``spec_pr`` requires foreman:spec-fix on "
        "the issue; ``impl_pr`` requires foreman:impl-fix. Default matches "
        "the pre-rescue behavior so any external caller that still omits "
        "the flag gets the same spec-side path."
    ),
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to foreman config (default: $FOREMAN_CONFIG or ~/.foreman/config.toml)",
)
def fix(
    issue_url: str,
    pr_url: str | None,
    project: str,
    target: str,
    config_path: Path | None,
) -> None:
    """Run the Fixer on an issue queued by the Reviewer.

    For ``--target spec_pr`` (default) the issue must carry
    ``foreman:spec-fix``; the Fixer derives the spec PR from the issue's
    ``foreman/issue-<N>`` branch, applies addressable Reviewer findings
    to the spec doc, commits + pushes, and advances the label based on
    outcome. For ``--target impl_pr`` the issue must carry
    ``foreman:impl-fix`` and the Fixer operates on the stacked
    ``foreman/impl-<N>`` branch instead.

    ``--pr-url`` is accepted but currently advisory — ``run_fixer``
    derives the PR from the branch convention. Passing it keeps the v3
    dispatch argv self-describing in audit logs.
    """
    cfg_path = config_path or _default_config_path()
    cfg = load_config(cfg_path)
    provider = AnthropicSDKProvider()
    # ``pr_url`` not yet plumbed into ``run_fixer`` — accepting it on the
    # CLI keeps the v3 reconciler's argv shape uniform; threading it
    # through the role is a separate change (out of scope for the
    # Stage-2 action split). Keep the read so linters don't flag it as
    # an unused arg.
    _ = pr_url
    result = asyncio.run(
        run_fixer(
            issue_url=issue_url,
            config=cfg,
            project_name=project,
            worktrees_root=_default_worktrees_root(),
            provider=provider,
            target=target,
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


def _resolve_shutdown_sentinel_path(config: Config | None) -> Path:
    """Return the path ``daemon stop`` writes (and the reconciler polls).

    ``FOREMAN_SHUTDOWN_SENTINEL_PATH`` env var wins over config so tests
    + operators can redirect without editing the config file. When
    ``config`` is ``None`` (e.g., ``daemon stop`` called on a host
    without a config file), falls back to the hardcoded default so the
    sentinel write still succeeds. An empty-string env value is
    treated as unset.
    """
    env_override = os.environ.get("FOREMAN_SHUTDOWN_SENTINEL_PATH") or None
    if env_override is not None:
        return Path(env_override).expanduser()
    if config is None:
        return Path("~/.foreman/shutdown-requested").expanduser()
    return Path(config.reconciler.shutdown_sentinel_path).expanduser()


def _resolve_reload_sentinel_path(config: Config | None) -> Path:
    """Return the path ``daemon reload`` writes (and the reconciler polls).

    ``FOREMAN_RELOAD_SENTINEL_PATH`` env var wins over config so tests
    + operators can redirect without editing the config file. When
    ``config`` is ``None`` (e.g., ``daemon reload`` called on a host
    without a config file), falls back to the hardcoded default so the
    sentinel write still succeeds. An empty-string env value is
    treated as unset.
    """
    env_override = os.environ.get("FOREMAN_RELOAD_SENTINEL_PATH") or None
    if env_override is not None:
        return Path(env_override).expanduser()
    if config is None:
        return Path("~/.foreman/reload-requested").expanduser()
    return Path(config.reconciler.reload_sentinel_path).expanduser()


def _build_reconciler_projects(config: Config) -> tuple[Any, ...]:
    """Build the ``ReconcilerProject`` tuple from ``config.projects``.

    Splits each ``ProjectConfig.repo`` ("owner/name") into owner + repo and
    resolves the effective auto-merge flags (global default + per-project
    override). Both ``daemon_v3_start`` and the reload-callback closure go
    through this helper so the project-resolution logic cannot drift across
    the startup path and the reload path.

    Raises ``click.ClickException`` when any project's ``repo`` is malformed
    (missing the ``/`` separator), surfacing the bad config to the operator
    rather than crashing the daemon mid-tick.
    """
    from foreman.reconciler import ReconcilerProject

    projects_list: list[ReconcilerProject] = []
    for proj_name, proj_cfg in config.projects.items():
        if "/" not in proj_cfg.repo:
            raise click.ClickException(
                f"project {proj_name!r} has malformed repo {proj_cfg.repo!r} "
                "(expected 'owner/name')"
            )
        owner, repo = proj_cfg.repo.split("/", 1)
        projects_list.append(
            ReconcilerProject(
                name=proj_name,
                owner=owner,
                repo=repo,
                auto_merge_spec=config.reconciler.effective_auto_merge_spec(proj_cfg),
                auto_merge_impl=config.reconciler.effective_auto_merge_impl(proj_cfg),
                merge_mechanism=config.reconciler.effective_merge_mechanism(proj_cfg),
            )
        )
    return tuple(projects_list)


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

    Graceful shutdown is requested two ways: (1) Ctrl-C / SIGTERM is
    caught by the in-loop signal handlers below (POSIX foreground only —
    on Windows ``os.kill(pid, SIGTERM)`` from ``foreman daemon stop`` maps
    to ``TerminateProcess``, which delivers no signal, so the handler
    can't fire); (2) ``foreman daemon stop`` writes a sentinel file at
    ``reconciler.shutdown_sentinel_path`` which the tick loop polls and
    consumes — the cross-platform path that actually works on Windows.
    """
    import logging

    from foreman.daemon_lock import DaemonLock, LockAcquisitionError
    from foreman.logging_setup import configure_daemon_logging
    from foreman.reconciler import ExecutionLog, Reconciler

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
    # resolve_state_dir() honors FOREMAN_STATE_DIR (set by compose for
    # the container at /foreman/state, mounted as a named volume so the
    # log survives `docker compose down`) with ~/.foreman fallback.
    from foreman.reconciler.v3_host import resolve_state_dir
    v3_log_path = resolve_state_dir() / "v3-daemon.log"
    configure_daemon_logging(
        log_path=v3_log_path,
        level=config.daemon.log_level,
    )

    lock_path = Path(os.path.expanduser(config.reconciler.lock_path))
    try:
        with DaemonLock(lock_path):
            # Clean up a stale shutdown sentinel from a prior `daemon stop`.
            # Without this, a sentinel left behind by a no-op stop (or by a
            # stop that raced ahead of cleanup) would be polled on the very
            # first tick and shut the new daemon down instantly. We own the
            # lock at this point, so no other daemon can be writing the
            # sentinel concurrently — a stale file is unambiguously dead.
            shutdown_sentinel_path = _resolve_shutdown_sentinel_path(config)
            if shutdown_sentinel_path.exists():
                logger = logging.getLogger("foreman")
                logger.warning(
                    "removing stale shutdown sentinel from prior daemon stop: %s",
                    shutdown_sentinel_path,
                )
                try:
                    shutdown_sentinel_path.unlink()
                except FileNotFoundError:
                    # Race with another `stop` is harmless — file's gone, which
                    # is what we wanted.
                    pass

            # Clean up a stale reload sentinel from a prior `daemon reload`.
            # Same rationale as the shutdown-sentinel cleanup above: a sentinel
            # left behind by a no-op reload (or by a prior edge case) would be
            # polled on the very first tick and trigger a config_reload of an
            # already-fresh config. Reloading a fresh config is a harmless
            # no-op, but the audit-log row would confuse anyone tracing daemon
            # activity. Cleanup runs INSIDE the DaemonLock block so no second
            # `daemon reload` can race the unlink.
            reload_sentinel_path = _resolve_reload_sentinel_path(config)
            if reload_sentinel_path.exists():
                logger = logging.getLogger("foreman")
                logger.warning(
                    "removing stale reload sentinel from prior daemon reload: %s",
                    reload_sentinel_path,
                )
                try:
                    reload_sentinel_path.unlink()
                except FileNotFoundError:
                    # Race with another `reload` is harmless — file's gone.
                    pass

            db_path = Path(os.path.expanduser(config.reconciler.db_path))
            log = ExecutionLog(db_path)
            log.init()

            recovered = log.recover_orphaned()
            if recovered > 0:
                click.echo(
                    f"recovered {recovered} orphaned running row(s) from prior daemon"
                )

            # Project tuple is resolved by the module-level helper so the
            # reload-callback path (below) goes through the same code, and
            # malformed ``owner/name`` repos raise the same ClickException
            # whether they're surfaced at startup or on a later reload.
            projects = _build_reconciler_projects(config)

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
            gh, host = _build_v3_gh_and_host(config, log)

            # Reload callback: when ``foreman daemon reload`` writes the
            # sentinel, the reconciler invokes this closure at the top of
            # its next tick to obtain a fresh project tuple. The closure
            # re-reads ``cfg_path`` from disk every call (no caching) so
            # operator edits to ``~/.foreman/config.toml`` show up the
            # next time reload is requested. Bubbling exceptions are
            # caught by the reconciler's failure-tolerance contract — see
            # ``Reconciler._apply_reload``.
            def _reload_callback() -> tuple[Any, ...]:
                fresh_config = load_config(cfg_path)
                return _build_reconciler_projects(fresh_config)

            reconciler = Reconciler(
                projects=projects,
                log=log,
                gh=gh,
                host=host,
                dry_run=dry_run,
                alert_after_n_failures=config.reconciler.alert_after_n_failures,
                poll_interval_seconds=config.reconciler.poll_interval_seconds,
                shutdown_sentinel_path=config.reconciler.shutdown_sentinel_path,
                reload_callback=_reload_callback,
                reload_sentinel_path=config.reconciler.reload_sentinel_path,
            )

            async def _shutdown_watcher(
                stop_event: asyncio.Event,
            ) -> None:
                await stop_event.wait()
                await reconciler.shutdown()

            async def _run() -> None:
                """Daemon main coroutine.

                Installs SIGTERM/SIGINT handlers for graceful shutdown.
                On POSIX, ``loop.add_signal_handler`` is preferred — handlers
                run inside the asyncio loop thread.  On Windows, that call
                raises ``NotImplementedError``; we fall back to
                ``signal.signal``, which fires the handler in the main thread
                from outside the loop, then bridge into the loop via
                ``loop.call_soon_threadsafe``.  Either way, Ctrl-C and a
                SIGTERM from ``foreman daemon stop`` complete in-flight ticks
                before exiting.
                """
                loop = asyncio.get_event_loop()
                stop_event = asyncio.Event()

                def _signal_handler() -> None:
                    stop_event.set()

                if sys.platform == "win32":
                    # Windows: loop.add_signal_handler raises
                    # NotImplementedError. signal.signal works but invokes the
                    # handler from a different context, so bridge back into
                    # the loop with call_soon_threadsafe.
                    def _windows_handler(signum: int, frame: Any) -> None:
                        loop.call_soon_threadsafe(_signal_handler)

                    signal.signal(signal.SIGINT, _windows_handler)
                    signal.signal(signal.SIGTERM, _windows_handler)
                else:
                    try:
                        loop.add_signal_handler(signal.SIGTERM, _signal_handler)
                        loop.add_signal_handler(signal.SIGINT, _signal_handler)
                    except (NotImplementedError, RuntimeError):
                        # Embedded loops (e.g. some test harnesses) may not
                        # support signal handlers; degrade to KeyboardInterrupt
                        # delivered by asyncio.run.
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


def _build_v3_gh_and_host(config: Config, log: Any) -> tuple[Any, Any]:
    """Construct the real GHGraphQLClient + ReconcilerHost for v3 runtime.

    Pulls the planner App's installation token from IdentityRegistry for the
    GraphQL observer (read-only; planner has GraphQL scope). Wraps v2's
    GitHubDaemonHost for REST + adds dispatch_role subprocess spawn.

    The host is shared across all registered projects — V3GitHubHost is now
    project-agnostic; the executor passes ``project=ctx.snapshot.project``
    per call to ``dispatch_role``. IdentityRegistry + orchestrator wiring
    only need *some* project to bootstrap the installation token (the
    orchestrator App's token spans every repo the App is installed on), so
    we use the first registered project as the bootstrap seed.
    """
    from foreman.daemon_host import GitHubDaemonHost
    from foreman.identity import IdentityRegistry
    from foreman.reconciler.gh_graphql import HttpxGHGraphQLClient
    from foreman.reconciler.v3_host import V3GitHubHost, resolve_log_dir

    # Pick any registered project as the orchestrator-token bootstrap seed —
    # the orchestrator App's installation token spans every repo it's
    # installed on, so this is not a per-project decision.
    bootstrap_project_name = next(iter(config.projects))
    project_config = config.projects[bootstrap_project_name]
    # IdentityRegistry needs orchestrator config — GitHubDaemonHost's REST
    # methods call get_orchestrator_client() on every call. Mirrors the v2
    # daemon construction path elsewhere in this file.
    registry = IdentityRegistry(project_config, orchestrator=config.orchestrator)
    v2_host = GitHubDaemonHost(identity_registry=registry)

    # Use the planner App's token for the GraphQL observer (read-only).
    # Pass a supplier (not a static string) so each poll re-fetches the
    # current token from IdentityRegistry — App-installation tokens expire
    # after 1 hour, and the registry's _get_cached path transparently
    # re-mints when the cached one is near expiry. The pre-fix code captured
    # the token-at-startup permanently in httpx headers, so the daemon
    # silently 401'd ~1 hour after launch. See foreman#142.
    gh = HttpxGHGraphQLClient(token_supplier=lambda: registry.get_token("planner"))
    host = V3GitHubHost(
        v2_host=v2_host,
        log=log,
        role_dispatch_timeout_seconds=config.reconciler.role_dispatch_timeout_seconds,
        max_concurrent_dispatches=config.reconciler.max_concurrent_dispatches,
        # foreman#119: capture per-dispatch subprocess output under
        # <log_dir>/<role>/<issue>__<ts>Z.log so a failed Planner/
        # Worker/Fixer/Reviewer can be diagnosed without manually
        # replaying the dispatch. Path is also recorded in the
        # execution log's `details` so post-mortem is one command.
        # resolve_log_dir() honors FOREMAN_LOG_DIR (set by compose for
        # the container at /foreman/logs) with ~/.foreman/logs fallback.
        log_dir=resolve_log_dir(),
    )
    return gh, host


@daemon.command("stop")
def daemon_stop() -> None:
    """Signal a running daemon to stop and wait for clean exit.

    Two-channel shutdown request:

    1. Writes ``reconciler.shutdown_sentinel_path`` (default
       ``~/.foreman/shutdown-requested``). The v3 reconciler polls this
       file each tick, deletes it on detection, and triggers graceful
       shutdown. This is the cross-platform channel — on Windows it is
       the ONLY working mechanism because ``os.kill(pid, SIGTERM)`` maps
       to ``TerminateProcess`` (a hard kill that delivers no signal),
       which can't run the daemon's cleanup path.
    2. On POSIX, also reads the daemon's PID from the lock file
       (``DaemonLock`` writes the PID on acquisition) and sends SIGTERM
       as a faster signal — handler fires within milliseconds rather
       than waiting for the next tick. The lock file remains on disk
       after stop — its content is now stale but the OS lock is
       released when the daemon's fd closes, so the next ``daemon
       start`` succeeds and overwrites the PID.

    Either channel alone is sufficient; running both is belt-and-
    suspenders on POSIX. The sentinel-only Windows path tolerates up
    to ``reconciler.poll_interval_seconds`` of latency before the
    daemon notices.
    """
    try:
        config: Config | None = _load_config_from_env()
    except (FileNotFoundError, OSError):
        config = None

    lock_path = _resolve_lock_path(config)

    # Check the lock-file gate BEFORE writing the sentinel. If no daemon
    # is running, leaving a sentinel on disk would silently kill the
    # next `daemon v3-start` — the reconciler polls the sentinel on its
    # first tick and shuts down immediately. The sentinel is only useful
    # when there's a live daemon to receive it.
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

    # Lock file exists AND we have a PID — there's a daemon to receive
    # the sentinel. Write it now. This is the cross-platform shutdown
    # channel (the only one on Windows, since SIGTERM there maps to
    # TerminateProcess and skips the daemon's cleanup path).
    sentinel_path = _resolve_shutdown_sentinel_path(config)
    try:
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel_path.write_text(
            f"requested by foreman daemon stop at {time.time()}\n",
            encoding="utf-8",
        )
        click.echo(f"shutdown requested via sentinel: {sentinel_path}")
    except OSError as exc:
        # Don't bail — the SIGTERM path below may still reach a POSIX
        # daemon. Surface the failure so the operator knows the v3
        # cross-platform path was skipped.
        click.echo(f"warning: could not write shutdown sentinel ({exc})", err=True)

    # Windows: skip os.kill(SIGTERM) entirely. On Windows it maps to
    # TerminateProcess — a hard kill that delivers no signal, defeating
    # the graceful-shutdown path the sentinel exists to enable. The
    # daemon will pick up the sentinel on its next tick.
    if sys.platform == "win32":
        click.echo(
            f"Windows: relying on sentinel only (pid {pid}). The v3 "
            f"reconciler will detect the sentinel on its next tick "
            f"(up to ``reconciler.poll_interval_seconds`` of latency)."
        )
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        click.echo(f"Pid {pid} not running (stale lock file).")
        return
    except OSError as exc:
        # Don't undo the sentinel — surface the failure and let the
        # tick-poller path complete the shutdown.
        click.echo(
            f"sentinel written but failed to send SIGTERM to pid {pid}: "
            f"{exc}. Daemon will still pick up the sentinel on its next tick.",
            err=True,
        )
        return
    click.echo(f"Sent SIGTERM to daemon pid {pid}; waiting for graceful shutdown.")

    # Poll for process death via os.kill(pid, 0). The daemon catches
    # SIGTERM and runs its cleanup before exiting; the OS releases the
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


@daemon.command("reload")
def daemon_reload() -> None:
    """Signal a running v3 daemon to re-read its config and update the
    project registry without restarting.

    Writes a sentinel file at ``reconciler.reload_sentinel_path`` (default
    ``~/.foreman/reload-requested``). The v3 reconciler polls the file at
    the top of each tick — when present, it re-reads
    ``~/.foreman/config.toml``, diffs the project registry, and starts
    reconciling newly-added projects + stops reconciling removed ones.
    In-flight role subprocesses for removed projects complete naturally.

    Latency: up to ``reconciler.poll_interval_seconds`` (default 60s).
    Sentinel-only on every platform — no SIGHUP — to keep the latency
    profile symmetric and the failure mode auditable (one channel).

    Gates on the lock file BEFORE writing the sentinel: if no daemon is
    running, the sentinel would silently fire on the next
    ``daemon v3-start``, which would log a confusing reload-of-an-already-
    fresh-config row. Same defense-in-depth pattern as ``daemon stop``.
    """
    try:
        config: Config | None = _load_config_from_env()
    except (FileNotFoundError, OSError):
        config = None

    lock_path = _resolve_lock_path(config)

    # Check the lock-file gate BEFORE writing the sentinel. If no daemon
    # is running, leaving a sentinel on disk would silently fire on the
    # next `daemon v3-start` — the reconciler polls the sentinel on its
    # first tick and would log a config_reload row for a fresh config.
    if not lock_path.exists():
        discover = (
            "tasklist | findstr foreman"
            if sys.platform == "win32"
            else "ps aux | grep foreman"
        )
        click.echo(
            f"No daemon lock file at {lock_path}. Either the daemon "
            f"was never started, or the lock file was removed. To find "
            f"a stray process: `{discover}`."
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

    # Lock file exists AND we have a PID — there's a daemon to receive
    # the sentinel. Write it now.
    sentinel_path = _resolve_reload_sentinel_path(config)
    try:
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel_path.write_text(
            f"requested by foreman daemon reload at {time.time()}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        click.echo(f"warning: could not write reload sentinel ({exc})", err=True)
        return
    click.echo(f"reload requested via sentinel: {sentinel_path}")


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
    """Load config from FOREMAN_CONFIG_PATH (container) / FOREMAN_CONFIG
    (host legacy) env var or default ~/.foreman/config.toml."""
    from foreman.config import resolve_config_path
    return load_config(resolve_config_path())


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
