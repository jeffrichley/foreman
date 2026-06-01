"""Foreman configuration — TOML loading with env-var override hierarchy.

Hierarchy (highest precedence first):
1. Env var (e.g., ``FOREMAN_PLANNER_APP_ID``)
2. Config file value (e.g., ``apps.planner_app_id`` in
   ``~/.foreman/config.toml``)

App ids are not secret on their own, but lookup through env vars matches the
pattern previously used for PATs and lets CI / Docker / one-off testing
inject them without touching the config file. The private-key path is
config-file-only (its file content is the secret; chmod-600 the .pem itself).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class AdminConfig(BaseModel):
    """Admin identity (Jeff's PAT) used for ``foreman project add`` ops."""

    github_token_env: str = "FOREMAN_ADMIN_TOKEN"


class OrchestratorConfig(BaseModel):
    """Orchestrator-bot identity used by the daemon for host operations.

    The orchestrator handles label management, PR merging, and polling
    searches — actions that don't belong to any specific role bot and
    historically would have used Jeff's PAT. A dedicated bot identity
    gives every Foreman action a proper audit trail.

    Resolved via JWT-from-private-key at daemon startup, same pattern
    as the per-role apps.
    """

    app_id_env: str = "FOREMAN_ORCHESTRATOR_APP_ID"
    app_id: int | None = None
    private_key_path: str | None = None

    def resolve_app_id(self) -> int:
        env_value = os.environ.get(self.app_id_env)
        if env_value:
            return int(env_value)
        if self.app_id is not None:
            return self.app_id
        raise RuntimeError(
            f"No orchestrator app_id: env var {self.app_id_env} not set "
            "and orchestrator.app_id not in config file"
        )

    def resolve_private_key_path(self) -> Path:
        if not self.private_key_path:
            raise RuntimeError(
                "orchestrator.private_key_path not set in config file"
            )
        return Path(self.private_key_path)


class DaemonConfig(BaseModel):
    """Daemon runtime configuration.

    ``max_concurrent_workers`` is a v1 forward-compat knob — only ``1`` is
    valid in v1. The lock infrastructure tolerates higher values but the
    daemon code is not yet audited for multi-worker safety. v2 will lift
    this validation.
    """

    poll_interval_seconds: int = Field(default=30, ge=5)
    max_concurrent_workers: int = Field(default=1)
    log_path: str = Field(default="~/.foreman/daemon.log")
    log_level: str = Field(default="INFO")
    sqlite_path: str = Field(default="~/.foreman/foreman.sqlite")
    role_dispatch_timeout_seconds: int = Field(default=600, ge=30)

    @field_validator("max_concurrent_workers")
    @classmethod
    def _validate_max_workers(cls, v: int) -> int:
        if v != 1:
            raise ValueError(
                "daemon.max_concurrent_workers must be 1 in v1; "
                "multi-worker concurrency is deferred"
            )
        return v


class AppsConfig(BaseModel):
    """Per-role GitHub App credentials.

    Walking skeleton wires planner + reviewer + fixer + worker. Each new
    role added during thickening adds a triple here; resolution methods
    raise a clear RuntimeError when an unconfigured role is requested.

    Each role needs:
      * ``<role>_app_id_env``: env var name holding the App id (stringified int).
      * ``<role>_app_id``: optional fallback if env var unset.
      * ``<role>_private_key_path``: filesystem path to the App's RSA private
        key (PEM). Typical location: ``~/.foreman/keys/<role>.pem``.
    """

    planner_app_id_env: str = "FOREMAN_PLANNER_APP_ID"
    planner_app_id: int | None = None
    planner_private_key_path: str | None = None

    reviewer_app_id_env: str = "FOREMAN_REVIEWER_APP_ID"
    reviewer_app_id: int | None = None
    reviewer_private_key_path: str | None = None

    fixer_app_id_env: str = "FOREMAN_FIXER_APP_ID"
    fixer_app_id: int | None = None
    fixer_private_key_path: str | None = None

    worker_app_id_env: str = "FOREMAN_WORKER_APP_ID"
    worker_app_id: int | None = None
    worker_private_key_path: str | None = None

    def resolve_planner_app_id(self) -> int:
        env_value = os.environ.get(self.planner_app_id_env)
        if env_value:
            return int(env_value)
        if self.planner_app_id is not None:
            return self.planner_app_id
        raise RuntimeError(
            f"No planner app_id: env var {self.planner_app_id_env} not set "
            "and apps.planner_app_id not in config file"
        )

    def resolve_planner_private_key_path(self) -> Path:
        if not self.planner_private_key_path:
            raise RuntimeError("apps.planner_private_key_path not set in config file")
        return Path(self.planner_private_key_path)

    def resolve_reviewer_app_id(self) -> int:
        env_value = os.environ.get(self.reviewer_app_id_env)
        if env_value:
            return int(env_value)
        if self.reviewer_app_id is not None:
            return self.reviewer_app_id
        raise RuntimeError(
            f"No reviewer app_id: env var {self.reviewer_app_id_env} not set "
            "and apps.reviewer_app_id not in config file"
        )

    def resolve_reviewer_private_key_path(self) -> Path:
        if not self.reviewer_private_key_path:
            raise RuntimeError("apps.reviewer_private_key_path not set in config file")
        return Path(self.reviewer_private_key_path)

    def resolve_fixer_app_id(self) -> int:
        env_value = os.environ.get(self.fixer_app_id_env)
        if env_value:
            return int(env_value)
        if self.fixer_app_id is not None:
            return self.fixer_app_id
        raise RuntimeError(
            f"No fixer app_id: env var {self.fixer_app_id_env} not set "
            "and apps.fixer_app_id not in config file"
        )

    def resolve_fixer_private_key_path(self) -> Path:
        if not self.fixer_private_key_path:
            raise RuntimeError("apps.fixer_private_key_path not set in config file")
        return Path(self.fixer_private_key_path)

    def resolve_worker_app_id(self) -> int:
        env_value = os.environ.get(self.worker_app_id_env)
        if env_value:
            return int(env_value)
        if self.worker_app_id is not None:
            return self.worker_app_id
        raise RuntimeError(
            f"No worker app_id: env var {self.worker_app_id_env} not set "
            "and apps.worker_app_id not in config file"
        )

    def resolve_worker_private_key_path(self) -> Path:
        if not self.worker_private_key_path:
            raise RuntimeError("apps.worker_private_key_path not set in config file")
        return Path(self.worker_private_key_path)


class ProjectConfig(BaseModel):
    """Per-project configuration.

    ``check_command`` is the project's verification command — what the
    Worker is instructed to run before claiming an implementation done,
    and what the orchestrator re-runs after the Worker returns as a
    belt-and-suspenders ground-truth check. Defaults to ``"just check"``
    when ``None``; projects that use a different runner (e.g.
    ``"make test"``, ``"npm test"``, ``"pytest -q"``) override it here.
    """

    repo: str = Field(..., description="GitHub repo in 'owner/name' form")
    local_clone_path: str = Field(
        ..., description="Local path to the repo's clone (worktrees branch from here)"
    )
    apps: AppsConfig = Field(default_factory=AppsConfig)
    check_command: str | None = Field(
        default=None,
        description=(
            "Project's verification command. Worker runs this in its "
            "worktree before claiming done; orchestrator re-runs it as "
            "ground-truth. Resolves to 'just check' when None."
        ),
    )
    dev_base_branch: str | None = Field(
        default=None,
        description=(
            "Optional alternate branch to use as the base when creating spec "
            "worktrees, instead of origin/<default_branch>. Set this when the project's "
            "active development line lives on a feature branch (e.g., during a walking-"
            "skeleton phase) rather than on main. The branch must exist on origin; "
            "Foreman will fetch it before branching."
        ),
    )
    auto_merge_spec: bool = Field(
        default=False,
        description=(
            "When True, daemon auto-merges spec PRs that reach foreman:spec-ready. "
            "When False (default), ticket parks at spec-ready awaiting human merge."
        ),
    )
    auto_merge_impl: bool = Field(
        default=False,
        description=(
            "When True, daemon auto-merges impl PRs that reach foreman:ready-for-merge. "
            "When False (default), ticket parks at ready-for-merge awaiting human merge."
        ),
    )


class Config(BaseModel):
    """Top-level Foreman config."""

    admin: AdminConfig = Field(default_factory=AdminConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)


def load_config(path: Path | str) -> Config:
    """Load + validate Foreman config from a TOML file."""
    p = Path(path)
    with p.open("rb") as f:
        raw = tomllib.load(f)
    return Config.model_validate(raw)
