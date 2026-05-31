"""Foreman configuration — TOML loading with env-var override hierarchy.

Hierarchy (highest precedence first):
1. Env var (e.g., FOREMAN_PLANNER_BOT_TOKEN)
2. Config file value (e.g., bots.planner_token in ~/.foreman/config.toml)

Tokens are secrets — env-var precedence lets CI / Docker / one-off testing
inject them without touching the config file.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class AdminConfig(BaseModel):
    """Admin identity (Jeff's PAT) used for `foreman project add` ops."""

    github_token_env: str = "FOREMAN_ADMIN_TOKEN"


class BotConfig(BaseModel):
    """Per-role bot credentials with env-var override.

    For walking skeleton: only planner is needed. Reviewer/fixer/worker fields
    are placeholders for thickening; resolution methods will raise until set.
    """

    planner_env: str = "FOREMAN_PLANNER_BOT_TOKEN"
    planner_token: str | None = None

    def resolve_planner_token(self) -> str:
        env_value = os.environ.get(self.planner_env)
        if env_value:
            return env_value
        if self.planner_token:
            return self.planner_token
        raise RuntimeError(
            f"No planner token: env var {self.planner_env} not set and "
            "bots.planner_token not in config file"
        )


class ProjectConfig(BaseModel):
    """Per-project configuration."""

    repo: str = Field(..., description="GitHub repo in 'owner/name' form")
    local_clone_path: str = Field(
        ..., description="Local path to the repo's clone (worktrees branch from here)"
    )
    bots: BotConfig = Field(default_factory=BotConfig)


class Config(BaseModel):
    """Top-level Foreman config."""

    admin: AdminConfig = Field(default_factory=AdminConfig)
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)


def load_config(path: Path | str) -> Config:
    """Load + validate Foreman config from a TOML file."""
    p = Path(path)
    with p.open("rb") as f:
        raw = tomllib.load(f)
    return Config.model_validate(raw)
