"""Foreman v4 CLI — typer app.

Command groups land in sibling files (ps.py, show.py, etc.); each
registers itself with this top-level ``app``. The console script
entry point is foreman.v4.cli:main (set in Task 6.6), which imports +
invokes this app.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

# Role-command delegates — framework-agnostic run_<role>_cli functions
# in foreman.roles.* (survival set, allowed to import from foreman.v4.*).
# Bound at module level so tests can patch
# `foreman.v4.cli.run_<role>_cli` cleanly.
from foreman.roles.fixer import run_fixer_cli
from foreman.roles.planner import run_planner_cli
from foreman.roles.reviewer import run_reviewer_cli
from foreman.roles.worker import run_worker_cli
from foreman.v4.cli.daemon import (
    cmd_daemon_reload,
    cmd_daemon_start,
    cmd_daemon_status,
    cmd_daemon_stop,
)
from foreman.v4.cli.log import cmd_log
from foreman.v4.cli.mutations import (
    cmd_drop,
    cmd_hold,
    cmd_resume,
    cmd_retry,
    cmd_set_state,
    cmd_skip,
)
from foreman.v4.cli.ps import cmd_ps
from foreman.v4.cli.queue import cmd_queue
from foreman.v4.cli.show import cmd_show

__version__ = "0.4.0"

app = typer.Typer(
    name="foreman",
    help="Foreman v4 — autonomous-loop coordinator",
    no_args_is_help=True,
)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", help="Print version and exit",
    ),
) -> None:
    if version:
        typer.echo(f"foreman {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


# Query + log + mutation commands wired. Daemon lifecycle commands land in
# the daemon_app sub-typer below.
app.command("ps")(cmd_ps)
app.command("show")(cmd_show)
app.command("queue")(cmd_queue)
app.command("log")(cmd_log)
app.command("hold")(cmd_hold)
app.command("resume")(cmd_resume)
app.command("retry")(cmd_retry)
app.command("skip")(cmd_skip)
app.command("drop")(cmd_drop)
app.command("set-state")(cmd_set_state)


daemon_app = typer.Typer(name="daemon", help="Daemon lifecycle")
app.add_typer(daemon_app)

daemon_app.command("start")(cmd_daemon_start)
daemon_app.command("stop")(cmd_daemon_stop)
daemon_app.command("reload")(cmd_daemon_reload)
daemon_app.command("status")(cmd_daemon_status)


# Role commands — delegate to the run_<role>_cli imports at the top of
# the file; bound there so tests can patch the module-level name.
@app.command("plan")
def cmd_plan(
    project: str = typer.Option(..., "--project"),
    issue_number: int = typer.Option(..., "--issue-number"),
) -> None:
    """Run the v4 Planner: emit FOREMAN_OUTCOME; exit code carries success/failure."""
    raise typer.Exit(code=run_planner_cli(project=project, issue_number=issue_number))


@app.command("review")
def cmd_review(
    project: str = typer.Option(..., "--project"),
    issue_number: int = typer.Option(..., "--issue-number"),
    target: str = typer.Option(..., "--target", help="spec|impl"),
) -> None:
    """Run the v4 Reviewer (target-aware): emit FOREMAN_OUTCOME; exit code carries verdict."""
    raise typer.Exit(code=run_reviewer_cli(
        project=project, issue_number=issue_number, target=target,
    ))


@app.command("fix")
def cmd_fix(
    project: str = typer.Option(..., "--project"),
    issue_number: int = typer.Option(..., "--issue-number"),
    target: str = typer.Option(..., "--target", help="spec|impl"),
) -> None:
    """Run the v4 Fixer (target-aware): emit FOREMAN_OUTCOME; exit code carries verdict."""
    raise typer.Exit(code=run_fixer_cli(
        project=project, issue_number=issue_number, target=target,
    ))


@app.command("implement")
def cmd_implement(
    project: str = typer.Option(..., "--project"),
    issue_number: int = typer.Option(..., "--issue-number"),
) -> None:
    """Run the v4 Worker: emit FOREMAN_OUTCOME; exit code carries verdict."""
    raise typer.Exit(code=run_worker_cli(
        project=project, issue_number=issue_number,
    ))


_DEFAULT_CONFIG = Path.home() / ".foreman" / "v4" / "config.toml"


def main() -> None:
    """Console-script entry point. Loads config, bootstraps the object
    graph, then invokes the typer app with the prepared context.
    """
    # Local imports keep the typer app importable for tests without
    # requiring PyGithub or any App credentials to be configured.
    from github import Github

    from foreman.v4.bootstrap import bootstrap_cli_context
    from foreman.v4.config import load_config
    from foreman.v4.identity import V4IdentityRegistry
    from foreman.v4.pygithub_git_provider import PyGithubGitProvider

    config_path = Path(os.environ.get("FOREMAN_V4_CONFIG", _DEFAULT_CONFIG))
    config = load_config(config_path)

    # Single-installation-per-role-bot assumption (see
    # ``foreman.v4.identity`` module docstring): the orchestrator's App
    # installation-id lookup needs *some* repo the App is installed in.
    # v4 assumes every v4-managed repo shares the same per-role App
    # installation, so any project's repo works; we pick the first
    # project's repo deterministically. A zero-project config can't
    # mint orchestrator tokens, so refuse to start with a clear message.
    if not config.projects:
        raise RuntimeError(
            "V4Config has no projects — daemon cannot identify which "
            "repo to use for App installation lookup. Add at least one "
            "[[projects]] block.",
        )
    identity = V4IdentityRegistry(
        apps=config.apps,
        orchestrator=config.orchestrator,
        installation_repo=config.projects[0].repo,
    )

    def _git_factory(repo: str) -> PyGithubGitProvider:
        # Reuse the identity layer's "orchestrator" token for read paths;
        # role-specific tokens flow through SubprocessRoleDispatcher.
        token = identity.get_role_token("orchestrator")
        return PyGithubGitProvider(github=Github(token), repo_full_name=repo)

    ctx = bootstrap_cli_context(
        config=config,
        identity=identity,
        git_provider_factory=_git_factory,
    )
    app(obj=ctx)
