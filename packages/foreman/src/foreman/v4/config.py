"""V4Config — TOML-loaded daemon configuration.

This IS a pydantic model: config is a boundary surface. TOML on disk
is untrusted input the daemon parses at startup; pydantic's validation
catches typos, wrong types, invalid enum values before the daemon
half-starts and confuses everything downstream.

Schema:
  [daemon]
    db_path              - SQLite DB path
    log_dir              - directory for rich-stdout + jsonl
    log_level            - default INFO
    tick_seconds         - cadence between Poller ticks (default 30)
    max_in_flight        - Single concurrency knob: sizes BOTH the QM
                           in-flight cap AND the WorkerPool
                           ThreadPoolExecutor. One number so a stuck-
                           ticket scenario can't strand pool threads
                           while the QM still has slots (or vice
                           versa). Default 1 (serial); opt in to higher
                           after dogfood stability.
    role_timeout_seconds - Subprocess timeout per role invocation
                           (default 600 = 10 min). Phase 7.5 threads
                           this to SubprocessRoleDispatcher.
    merge_mechanism      - "queue" (default) | "merge" | "squash" | "rebase"

  [apps.planner]
    app_id           - GitHub App numeric ID
    private_key_path - PEM file path on disk (loader does NOT read it;
                       main() converts the string + reads the PEM at
                       IdentityRegistry construction time)

  [apps.reviewer]
    ... same shape ...

  [apps.fixer]
    ... same shape ...

  [apps.worker]
    ... same shape ...

  Note: all four [apps.<role>] blocks are REQUIRED. A missing block
  raises ValidationError at load time; the daemon literally cannot
  run without per-role identity wiring, so refusing to start is the
  right failure mode.

  [orchestrator]
    app_id           - GitHub App numeric ID for the orchestrator-level
                       (non-role) bot.
    private_key_path - PEM file path on disk for the orchestrator App.
                       The loader does NOT read it; main() converts the
                       string + reads the PEM at IdentityRegistry
                       construction time.

  Note: the [orchestrator] block is REQUIRED. Task 8.4 pivoted away
  from a long-lived env-var PAT to short-lived App-installation
  tokens — the Google-style default — so the daemon literally needs an
  app_id + private_key_path to mint orchestrator-level tokens.

  [[projects]]
    name              - project slug
    repo              - "owner/name"
    local_clone_path  - filesystem path
    trigger_label     - GH label (default "foreman:plan")
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    repo: str
    local_clone_path: str
    trigger_label: str = "foreman:plan"


class AppCredentials(BaseModel):
    """GitHub App identity for a single role.

    ``private_key_path`` is a string, not a ``Path``: the loader does
    not read the PEM file. main() (Task 8.4) handles the file read at
    ``IdentityRegistry`` construction time so config-load stays a pure
    validation step.
    """

    model_config = ConfigDict(extra="forbid")
    app_id: int
    private_key_path: str


class AppsConfig(BaseModel):
    """Per-role GitHub App credentials.

    All four roles are required — the daemon's whole identity model is
    one App per role, so a missing role IS a misconfiguration that
    should refuse to start.
    """

    model_config = ConfigDict(extra="forbid")
    planner: AppCredentials
    reviewer: AppCredentials
    fixer: AppCredentials
    worker: AppCredentials


class OrchestratorConfig(BaseModel):
    """Orchestrator-level (non-role) GitHub App identity.

    Task 8.4 pivoted away from long-lived env-var PATs to short-lived,
    auto-refreshing App installation tokens — the same shape as the
    per-role :class:`AppCredentials`. The orchestrator still gets its
    own App (separate identity for non-role operations like reading
    open issues during polling); the token-minting flow goes through
    :func:`foreman.auth.mint_installation_token` at runtime.

    ``private_key_path`` is a string, not a ``Path``: the loader does
    not read the PEM file. main() (Task 8.4) hands it to
    :class:`~foreman.v4.identity.V4IdentityRegistry`, which reads the
    PEM at token-mint time.
    """

    model_config = ConfigDict(extra="forbid")
    app_id: int
    private_key_path: str


class V4Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    db_path: str
    log_dir: str
    log_level: str = "INFO"
    tick_seconds: float = 30.0
    max_in_flight: int = 1
    role_timeout_seconds: int = 600
    merge_mechanism: Literal["queue", "merge", "squash", "rebase"] = "queue"
    apps: AppsConfig
    # Task 8.4: orchestrator is REQUIRED (no default). Google-style App
    # installation credentials are the only supported path — there is no
    # PAT-env-var fallback. A missing [orchestrator] block raises
    # ValidationError at load time, same as the per-role [apps.*] blocks.
    orchestrator: OrchestratorConfig
    projects: list[ProjectConfig] = Field(default_factory=list)


def load_config(path: Path) -> V4Config:
    """Parse the TOML at ``path`` and validate as V4Config.

    Invalid TOML / missing required fields / wrong types raise pydantic
    ValidationError. The daemon's startup catches these and exits with
    a useful message; this function deliberately does not swallow.
    """
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    daemon = raw.get("daemon", {})
    projects = raw.get("projects", [])
    # ``apps`` and ``orchestrator`` are both required (Task 8.4).
    # When the block is absent we leave the key out of the payload so
    # Pydantic surfaces a clean ``Field required`` error naming the
    # missing field, instead of validating an empty dict against the
    # required-field schema (which would still fail but with messier
    # output mentioning every missing subfield).
    payload: dict[str, object] = {
        **daemon,
        "projects": projects,
    }
    if "apps" in raw:
        payload["apps"] = raw["apps"]
    if "orchestrator" in raw:
        payload["orchestrator"] = raw["orchestrator"]
    return V4Config.model_validate(payload)
