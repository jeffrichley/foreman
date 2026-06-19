"""``foreman init <project>`` — bootstrap a project repo for v4.

Creates the v4 foreman state labels, verifies bot installations,
validates the local clone, and writes the per-project
``.foreman/INSTRUCTIONS.md`` template. Does NOT touch any config file —
v4 config is the source of truth; this command consumes it.

Delegates to helpers in :mod:`foreman.init` for the label / bot /
instructions work. ``foreman.init`` is in the v4 survival set per the
isolation guard (``tests/v4/test_isolation.py``); after Phase 8d.9 the
helpers it exposes operate directly on :class:`V4Config` /
:class:`AppsConfig`, so this command no longer needs a v3 shim.
"""
from __future__ import annotations

import os
from pathlib import Path

import typer
from github import Auth, Github

from foreman.init import (
    _ROLE_NAMES,
    BotVerification,
    _check_instructions_committed,
    _ensure_labels,
    _validate_clone_path,
    _verify_bot_installation,
    _write_instructions_template,
)
from foreman.v4.config import load_config
from foreman.v4.identity import V4IdentityRegistry

# Default check-command threaded into the instructions template. Matches
# v3's ``foreman.init._DEFAULT_CHECK_COMMAND`` and ``foreman.roles.worker
# ._DEFAULT_CHECK_COMMAND`` — same default on both ends of the pipeline.
# After Phase 8b.2 grows ``ProjectConfig.check_command``, this command
# can read the per-project override; for 8b.1 we hardcode the default.
_DEFAULT_CHECK_COMMAND = "just check"

# Default V4 config path. Mirrors ``foreman.v4.cli._DEFAULT_CONFIG``;
# duplicated here as a local constant rather than imported to avoid a
# cycle with the top-level ``foreman.v4.cli`` package (which itself
# imports from this module).
_DEFAULT_CONFIG = Path.home() / ".foreman" / "v4" / "config.toml"


def cmd_init(
    project: str = typer.Argument(
        ..., help="Project name (must exist in V4Config.projects).",
    ),
) -> None:
    """Bootstrap a project repo for v4.

    Creates the 19 foreman labels on the GitHub repo, verifies that
    each role's GitHub App is installed there, validates the local
    clone path matches the configured repo, and writes the per-project
    ``.foreman/INSTRUCTIONS.md`` template into the clone. Idempotent —
    re-running on an already-initialized project leaves existing
    labels and instructions intact.

    Reads V4Config from ``$FOREMAN_V4_CONFIG`` (default
    ``~/.foreman/v4/config.toml``).
    """
    config_path = Path(os.environ.get("FOREMAN_V4_CONFIG", _DEFAULT_CONFIG))
    if not config_path.exists():
        typer.echo(f"V4 config not found at {config_path}", err=True)
        raise typer.Exit(code=1)
    config = load_config(config_path)

    project_cfg = next(
        (p for p in config.projects if p.name == project),
        None,
    )
    if project_cfg is None:
        known = [p.name for p in config.projects]
        typer.echo(
            f"project {project!r} not found in V4Config at {config_path}. "
            f"Known projects: {known}",
            err=True,
        )
        raise typer.Exit(code=1)

    clone_path = Path(project_cfg.local_clone_path)

    # Validate the clone path matches the configured repo. The v3
    # helper raises ValueError for every failure mode (missing dir,
    # not a git repo, origin remote mismatch); a single except clause
    # covers them all with the helper's specific message.
    try:
        _validate_clone_path(clone_path, project_cfg.repo)
    except ValueError as exc:
        typer.echo(f"clone validation failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    # Mint an orchestrator token for label + bot-installation API calls.
    # v4 identity is "single installation per role bot" (one App
    # installation across all v4-managed repos), so passing this
    # project's repo as ``installation_repo`` is the documented pattern.
    identity = V4IdentityRegistry(
        apps=config.apps,
        orchestrator=config.orchestrator,
        installation_repo=project_cfg.repo,
    )
    admin_token = identity.get_role_token("orchestrator")
    admin_client = Github(auth=Auth.Token(admin_token))

    # Write the per-project instructions template (idempotent — preserves
    # existing file if present).
    instructions_path, instructions_written = _write_instructions_template(
        clone_path=clone_path,
        repo_name=project_cfg.repo.split("/", 1)[1],
        check_command=_DEFAULT_CHECK_COMMAND,
    )
    instructions_dirty_warning = _check_instructions_committed(clone_path)

    # Create the 19 labels (idempotent — existing labels preserved).
    labels_created, labels_existing = _ensure_labels(
        client=admin_client, repo_slug=project_cfg.repo,
    )

    # Best-effort bot verification. v4 has shared apps at the V4Config
    # level — pass ``config.apps`` straight through; the helper now
    # speaks v4 :class:`AppsConfig` natively (Phase 8d.9).
    bot_verifications: list[BotVerification] = [
        _verify_bot_installation(role=role, apps=config.apps, repo_slug=project_cfg.repo)
        for role in _ROLE_NAMES
    ]

    # Print a human-readable summary. Shape mirrors v3 init's output
    # without the v3-config-write footer (v4 doesn't write to a config
    # file — V4Config is the source of truth, hand-edited by the
    # operator).
    typer.echo(f"\nforeman init: {project_cfg.repo}\n")
    typer.echo(f"  Project name:  {project}")
    typer.echo(f"  Clone path:    {project_cfg.local_clone_path}")
    typer.echo(
        f"  Labels:        {len(labels_created) + len(labels_existing)} total "
        f"({len(labels_created)} created, {len(labels_existing)} existed)",
    )
    typer.echo(
        f"  Instructions:  {instructions_path} "
        f"({'written' if instructions_written else 'preserved'})",
    )
    if instructions_dirty_warning:
        typer.echo(f"  WARNING: {instructions_dirty_warning}")
    typer.echo("\n  Bot verifications:")
    for v in bot_verifications:
        marker = "OK " if v.ok else "   "
        typer.echo(f"    [{marker}] {v.role:12s} {v.detail}")
