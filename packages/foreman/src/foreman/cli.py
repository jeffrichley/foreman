"""Foreman CLI — `foreman plan` + `foreman review` + `foreman fix` are the
walking-skeleton entries.

Thickening will add: `foreman work`, `foreman daemon ...`, `foreman project
add`, etc.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import click

from foreman.config import load_config
from foreman.providers.anthropic_sdk import AnthropicSDKProvider
from foreman.roles.fixer import run_fixer
from foreman.roles.planner import run_planner
from foreman.roles.reviewer import run_reviewer


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
    """Run the Reviewer on a spec PR opened by the Planner."""
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
    click.echo(
        f"{result.outcome}: {len(result.findings)} findings, "
        f"confidence={result.confidence}"
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
        f"{llm.outcome}: {result.attempt}/3 attempt, {addressed} fixed, "
        f"{unaddressed} unaddressed"
    )


def main() -> None:
    """Console-script entry point."""
    cli()
