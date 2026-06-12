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
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Valid values for ``merge_mechanism``. Lives at module scope so the
# config layer + the host's dispatch logic + tests can all reference
# the same authoritative tuple.
MergeMechanism = Literal["direct", "queue"]


def resolve_config_path() -> Path:
    """Resolve the foreman config TOML path, honoring container env var.

    Precedence (highest first):
      1. ``FOREMAN_CONFIG_PATH`` — set by docker-compose for containers.
      2. ``FOREMAN_CONFIG`` — legacy env var, kept for backward-compat
         with existing host workflows.
      3. ``~/.foreman/config.toml`` — host default.
    """
    env_value = os.environ.get("FOREMAN_CONFIG_PATH") or os.environ.get("FOREMAN_CONFIG")
    if env_value:
        return Path(env_value)
    return Path.home() / ".foreman" / "config.toml"


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
    lock_path: str = Field(default="~/.foreman/daemon.lock")
    # Wall-clock cap on a single role dispatch. ``None`` (the default)
    # disables the cap entirely — the daemon trusts the role to finish.
    # Set this only after empirical stats from ``node_runs.duration_ms``
    # show a defensible distribution per role; a cap chosen without data
    # cancels legitimate-but-slow LLM iterations and hurts more than it
    # helps. Surfaced 2026-06-01 when a 600s default cut a Worker mid-
    # impl on foreman#30.
    role_dispatch_timeout_seconds: int | None = Field(default=None, ge=30)
    # foreman#228 (runaway-burn defense): per-ticket-per-role
    # consecutive-failure rate-limit. After
    # ``rate_limit_max_consecutive_failures`` failures of the SAME role
    # on the SAME ticket within ``rate_limit_window_seconds``, the
    # reconciler stops re-dispatching and transitions the ticket to
    # ``foreman:needs-help`` (writing a reset sentinel so the human's
    # subsequent re-queue gives a fresh window). Per-ticket + per-role
    # isolation — failures on ticket A don't affect ticket B; Planner
    # failures don't affect Worker dispatch.
    #
    # N=3 / W=30min were criterion-checked against yesterday's burn
    # (foreman#227: ~1 dispatch/min for 2h52m = 171 total) — N=3 catches
    # it fast, W=30min leaves headroom for legitimate long dispatches
    # (Planner max_turns=1000 can run 10-20 minutes).
    rate_limit_max_consecutive_failures: int = Field(
        default=3,
        ge=1,
        description=(
            "N for the per-ticket-per-role consecutive-failure rate-limit "
            "(foreman#228). After N failures of the same role on the same "
            "ticket within ``rate_limit_window_seconds``, the reconciler "
            "transitions the ticket to ``foreman:needs-help`` and stops "
            "re-dispatching."
        ),
    )
    rate_limit_window_seconds: int = Field(
        default=1800,  # 30 minutes
        ge=60,
        le=24 * 3600,  # 24 hours — defense against a config typo
        # (e.g., rate_limit_window_seconds = 999999999) that would
        # silently disable the rate-limit by making the window so
        # large that count_recent_failures always reaches back to the
        # dawn of time. 24h ceiling is comfortably larger than any
        # legitimate window an operator would set while still being
        # human-meaningful.
        description=(
            "W (seconds) for the per-ticket-per-role consecutive-failure "
            "rate-limit (foreman#228). Failures older than W are ignored "
            "by the trip predicate. Capped at 24h to defend against a "
            "config typo silently disabling the rate-limit."
        ),
    )

    @field_validator("max_concurrent_workers")
    @classmethod
    def _validate_max_workers(cls, v: int) -> int:
        if v != 1:
            raise ValueError(
                "daemon.max_concurrent_workers must be 1 in v1; "
                "multi-worker concurrency is deferred"
            )
        return v


class ReconcilerConfig(BaseModel):
    """v3 reconciler knobs. Lives alongside DaemonConfig (which configures v2)."""

    db_path: str = Field(
        default="~/.foreman/reconciler.sqlite",
        description="sqlite path for the v3 execution log",
    )
    poll_interval_seconds: int = Field(
        default=60,
        ge=10,
        description="seconds between reconciler ticks",
    )
    retention_days: int = Field(
        default=30,
        ge=1,
        description="rows older than this are eligible for archive",
    )
    alert_after_n_failures: int = Field(
        default=3,
        ge=1,
        description="consecutive observer failures before yellow alert",
    )
    lock_path: str = Field(
        default="~/.foreman/reconciler.lock",
        description="File-based PID lock to prevent two reconciler daemons concurrently",
    )
    role_dispatch_timeout_seconds: int = Field(
        default=3600,  # 1 hour — Claude Code sessions typically run 10-30 min
        ge=60,
        description="Hard wall-clock ceiling for a dispatched role subprocess; SIGTERM on expiry.",
    )
    max_concurrent_dispatches: int = Field(
        default=1,
        ge=1,
        le=20,
        description=(
            "Max concurrent role subprocesses across all tickets "
            "(Planner+Worker+Reviewer+Fixer combined). Default is 1 — "
            "strictly sequential dispatch — so a fresh foreman install runs "
            "the safest behavior out of the box. Operators who have read "
            "the docs and decided they want parallelism set this explicitly "
            "to 2, 4, or higher. Each role subprocess can saturate bandwidth, "
            "disk, or LLM-context budgets on a dev box that hasn't been "
            "consciously sized for parallelism; opt-IN is the principle."
        ),
    )
    shutdown_sentinel_path: str = Field(
        default="~/.foreman/shutdown-requested",
        description=(
            "Cross-platform graceful-shutdown signal. ``foreman daemon stop`` "
            "writes this file; the reconciler polls it each tick and triggers "
            "graceful shutdown when present (deleting the file on detection). "
            "On POSIX, ``daemon stop`` ALSO sends SIGTERM for faster response; "
            "on Windows, the sentinel is the only mechanism — ``os.kill`` "
            "maps to ``TerminateProcess`` (a hard kill that delivers no signal) "
            "so the SIGTERM-handler path can't run."
        ),
    )
    reload_sentinel_path: str = Field(
        default="~/.foreman/reload-requested",
        description=(
            "Cross-platform config-reload signal. ``foreman daemon reload`` "
            "writes this file; the reconciler polls it at the TOP of each "
            "tick and re-reads ``~/.foreman/config.toml`` when present "
            "(deleting the file on detection). Newly-added projects begin "
            "reconciling on the same tick; removed projects stop emitting "
            "actions while any in-flight role subprocesses complete naturally. "
            "Sentinel-only by design (no SIGHUP on POSIX) so the latency "
            "profile is symmetric across platforms — operators see up to "
            "``poll_interval_seconds`` of latency before the reload lands."
        ),
    )
    auto_merge_spec: bool = Field(
        default=True,
        description=(
            "Global default for auto-merging spec PRs. Spec PRs are cheap "
            "to revert (just an .md file under docs/superpowers/specs/) "
            "so the daemon merges them by default to keep the autonomous "
            "loop moving. A per-project ``auto_merge_spec`` overrides this."
        ),
    )
    auto_merge_impl: bool = Field(
        default=False,
        description=(
            "Global default for auto-merging impl PRs. Impl PRs ship real "
            "code, so the daemon parks at ``foreman:ready-for-merge`` for "
            "human review by default. A per-project ``auto_merge_impl`` "
            "overrides this."
        ),
    )
    # foreman#228 (runaway-burn defense): mirror of the DaemonConfig
    # knobs at the v3 reconciler layer. The reconciler reads these
    # values per tick and threads them into every ``ActionContext``;
    # the rate-limit rules + RATE_LIMIT_TRIP executor read from there.
    #
    # The duplicate definition (here AND on ``DaemonConfig``) is
    # intentional: the ticket spec names ``DaemonConfig`` as the
    # configuration surface (operator mental model), while the v3
    # reconciler runs off ``ReconcilerConfig`` (architectural reality).
    # Surfaces match.
    rate_limit_max_consecutive_failures: int = Field(
        default=3,
        ge=1,
        description=(
            "N for the per-ticket-per-role consecutive-failure rate-limit "
            "(foreman#228). After N failures of the same role on the same "
            "ticket within ``rate_limit_window_seconds``, the reconciler "
            "transitions the ticket to ``foreman:needs-help`` and stops "
            "re-dispatching. Pepper criterion-checked N=3 as catching "
            "yesterday's burn (foreman#227: 1 dispatch/min for 2h52m = "
            "171 total) fast."
        ),
    )
    rate_limit_window_seconds: int = Field(
        default=1800,  # 30 minutes
        ge=60,
        le=24 * 3600,  # 24 hours — mirror of the DaemonConfig ceiling
        # so a config typo cannot silently disable the rate-limit at
        # the v3 daemon's reading surface either.
        description=(
            "W (seconds) for the per-ticket-per-role consecutive-failure "
            "rate-limit (foreman#228). 30min headroom leaves room for "
            "legitimate long dispatches (Planner max_turns=1000 ~ 10-20min). "
            "Capped at 24h to defend against a config typo silently "
            "disabling the rate-limit."
        ),
    )
    auto_fetch_on_poll: bool = Field(
        default=True,
        description=(
            "foreman#291: when True (the default), the reconciler "
            "fetches ``origin/<default-branch>`` for each registered "
            "project once per poll cycle so the container's local "
            "clone never grows more than ~``poll_interval_seconds`` "
            "stale. When False, falls back to the pre-#291 behavior — "
            "only the per-dispatch fetch in "
            "``WorktreeManager.create()`` refreshes the clone. Same "
            "operator-opts-OUT framing as ``max_concurrent_dispatches=1``: "
            "the default ships the safer behavior; paranoid / "
            "air-gapped / bandwidth-throttled environments flip the "
            "flag explicitly."
        ),
    )
    merge_mechanism: MergeMechanism = Field(
        default="direct",
        description=(
            "Global default for HOW the daemon merges a PR. Two modes:\n"
            "\n"
            "- ``direct``: call ``pr.merge()`` against the GitHub API. Works "
            "on any repo without setup, but fails with the "
            "``strict_required_status_checks_policy`` error (\"head branch is "
            "not up to date with base\") when main has moved since the PR "
            "opened. The daemon's retry loop then spins forever until an "
            "operator rebases the branch.\n"
            "- ``queue``: defer to GitHub MergeQueue, which rebases the PR "
            "onto current main, re-runs CI against the merged state, and "
            "merges atomically. Requires the base branch to have MergeQueue "
            "enabled in its branch-protection ruleset AND the orchestrator-"
            "bot App to hold the ``Merge queues: write`` permission. See "
            "foreman#158 for the rationale.\n"
            "\n"
            "Default is ``direct`` (matches the cap=1 principle: operators "
            "opt INTO the more sophisticated mechanism). A per-project "
            "override is honored via ``effective_merge_mechanism(project)``."
        ),
    )

    def effective_auto_merge_spec(self, project: ProjectConfig) -> bool:
        """Resolve the effective spec auto-merge for ``project``.

        Returns the per-project ``auto_merge_spec`` when set, else falls
        back to the global default. ``None`` on the project means "inherit".
        """
        if project.auto_merge_spec is not None:
            return project.auto_merge_spec
        return self.auto_merge_spec

    def effective_auto_merge_impl(self, project: ProjectConfig) -> bool:
        """Resolve the effective impl auto-merge for ``project``.

        Returns the per-project ``auto_merge_impl`` when set, else falls
        back to the global default. ``None`` on the project means "inherit".
        """
        if project.auto_merge_impl is not None:
            return project.auto_merge_impl
        return self.auto_merge_impl

    def effective_merge_mechanism(self, project: ProjectConfig) -> MergeMechanism:
        """Resolve the effective merge mechanism for ``project``.

        Returns the per-project ``merge_mechanism`` when set, else falls
        back to the global default. ``None`` on the project means "inherit
        from global." Symmetric with ``effective_auto_merge_*``.

        The action layer reads this per-call so a project can flip
        between ``direct`` and ``queue`` without restarting the daemon —
        the config reload sentinel (``foreman daemon reload``) re-loads
        the file and the next poll uses the new value.
        """
        if project.merge_mechanism is not None:
            return project.merge_mechanism
        return self.merge_mechanism


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
    auto_merge_spec: bool | None = Field(
        default=None,
        description=(
            "Per-project override for spec-PR auto-merge. ``True`` forces "
            "auto-merge, ``False`` forces park-for-review, ``None`` (the "
            "default) inherits ``ReconcilerConfig.auto_merge_spec`` via "
            "``ReconcilerConfig.effective_auto_merge_spec(project)``."
        ),
    )
    auto_merge_impl: bool | None = Field(
        default=None,
        description=(
            "Per-project override for impl-PR auto-merge. ``True`` forces "
            "auto-merge, ``False`` forces park-for-review, ``None`` (the "
            "default) inherits ``ReconcilerConfig.auto_merge_impl`` via "
            "``ReconcilerConfig.effective_auto_merge_impl(project)``."
        ),
    )
    merge_mechanism: MergeMechanism | None = Field(
        default=None,
        description=(
            "Per-project override for the merge mechanism. Set to "
            "``\"queue\"`` for projects whose base branch has MergeQueue "
            "enabled in its branch-protection ruleset; ``\"direct\"`` "
            "elsewhere. ``None`` (the default) inherits "
            "``ReconcilerConfig.merge_mechanism`` via "
            "``ReconcilerConfig.effective_merge_mechanism(project)``.\n"
            "\n"
            "Per-project granularity matters because MergeQueue is enabled "
            "at the GitHub repo level, not at foreman's. Different projects "
            "may have it set up at different times. The loop should keep "
            "working everywhere — projects opt IN to queue mechanism as "
            "the GH-side setup lands. See foreman#158."
        ),
    )
    max_fix_attempts: int = Field(
        default=3,
        ge=1,
        description=(
            "Retry budget for the Fixer's spec-fix cycle. The Nth+1 "
            "dispatch raises before any LLM run. Read at role-runtime by "
            "``foreman.roles.fixer``. ``foreman init`` still creates the "
            "default set of three ``foreman:fix-attempt-N`` labels; "
            "operators using a higher value should create additional "
            "labels manually (init-aware label creation is a follow-up)."
        ),
    )
    max_impl_attempts: int = Field(
        default=3,
        ge=1,
        description=(
            "Retry budget for the Worker's impl cycle. The Nth+1 dispatch "
            "raises before any LLM run. Read at role-runtime by "
            "``foreman.roles.worker``. ``foreman init`` still creates the "
            "default set of three ``foreman:impl-attempt-N`` labels; "
            "operators using a higher value should create additional "
            "labels manually (init-aware label creation is a follow-up)."
        ),
    )


class Config(BaseModel):
    """Top-level Foreman config."""

    admin: AdminConfig = Field(default_factory=AdminConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    reconciler: ReconcilerConfig = Field(default_factory=ReconcilerConfig)
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)


def load_config(path: Path | str) -> Config:
    """Load + validate Foreman config from a TOML file."""
    p = Path(path)
    with p.open("rb") as f:
        raw = tomllib.load(f)
    return Config.model_validate(raw)
