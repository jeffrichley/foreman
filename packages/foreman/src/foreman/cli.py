"""Foreman CLI — `foreman plan <issue-url>` is the walking-skeleton entry point.

Thickening will add: `foreman review`, `foreman work`, `foreman daemon ...`,
`foreman project add`, etc. Walking skeleton has just `plan`.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import click

from foreman.config import load_config
from foreman.providers.anthropic_sdk import AnthropicSDKProvider
from foreman.roles.planner import run_planner


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
    output = asyncio.run(
        run_planner(
            issue_url=issue_url,
            config=cfg,
            project_name=project,
            worktrees_root=_default_worktrees_root(),
            provider=provider,
        )
    )
    click.echo(f"Planner complete — PR #{output.pr_number} at {output.pr_url}")
    click.echo(f"Branch: {output.branch_name}")
    click.echo(f"Confidence: {output.confidence}")
    click.echo(f"Summary: {output.summary}")
    if output.considered_alternatives:
        click.echo("Considered alternatives:")
        for alt in output.considered_alternatives:
            click.echo(f"  - {alt}")


def main() -> None:
    """Console-script entry point."""
    cli()
