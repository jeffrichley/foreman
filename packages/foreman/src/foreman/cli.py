"""Foreman CLI — `foreman plan` + `foreman review` + `foreman fix` +
`foreman implement` are the walking-skeleton entries.

`foreman init` is the onboarding entry — runs the one-shot setup pass
that prepares a new target repo + writes the project's config block.

Thickening will add: `foreman daemon ...`, `foreman project add`, etc.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
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
    config = _load_config_from_env()
    asyncio.run(_daemon_run(config=config, max_iterations=max_iterations))


@daemon.command("stop")
def daemon_stop() -> None:
    """Signal a running daemon to stop. (v1: send SIGTERM to the pid)."""
    pid_path = Path("~/.foreman/daemon.pid").expanduser()
    if not pid_path.exists():
        click.echo("No daemon pid file found at ~/.foreman/daemon.pid.")
        return
    pid = int(pid_path.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        click.echo(f"Sent SIGTERM to daemon pid {pid}.")
    except ProcessLookupError:
        click.echo(f"Pid {pid} not running. Removing stale pid file.")
        pid_path.unlink()


@daemon.command("status")
def daemon_status() -> None:
    """Show daemon status — running / stopped."""
    pid_path = Path("~/.foreman/daemon.pid").expanduser()
    if not pid_path.exists():
        click.echo("Daemon: not running.")
        return
    pid = int(pid_path.read_text().strip())
    try:
        os.kill(pid, 0)
        click.echo(f"Daemon: running (pid {pid}).")
    except ProcessLookupError:
        click.echo(f"Daemon: stale pid file (pid {pid} dead). Run `foreman daemon stop` to clean.")


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
