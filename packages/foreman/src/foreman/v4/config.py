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
    clone_refresh_seconds - Min interval between per-poll origin/<default>
                           refreshes of each project clone (default 300 =
                           5 min). foreman#407.
    max_state_attempts   - Maximum consecutive same-state failures
                           before the state machine forces a transition
                           to NeedsHelp instead of running the role
                           again. Default 3. Runaway defense — the
                           dogfood loop went 86 attempts in 43 minutes
                           before this existed. Matches the shape of
                           max_fix_attempts / max_impl_attempts but
                           applies at state-machine granularity.

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
    check_command     - verification command for Worker (None → "just check")
    dev_base_branch   - alternate base branch for spec worktrees (None → origin/default)
    max_fix_attempts  - max fix cycles before NeedsHelp escalation (default 3)
    max_impl_attempts - max impl cycles before NeedsHelp escalation (default 3)
    auto_merge_impl   - when False (default), an approved impl PR parks at
                        ImplApproved for human merge; when True, foreman
                        auto-merges the impl PR (the historic behavior)
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Basic RFC-5321-ish email-shape regex per issue #347 spec. The schema
# only validates SHAPE — semantic validity (LDAP, GitHub account exists,
# MX record check) is the operator's responsibility.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OperatorIdentity(BaseModel):
    """One human operator identity (name + email) used in a commit trailer.

    Issue #347: the operator-identity primitive that backs both
    ``Supervised-by:`` (orchestration attribution) and ``Signed-off-by:``
    (DCO legal attestation). The same shape is used for the top-level
    block and (when set) per-project overrides.

    Both ``name`` and ``email`` are required and must be non-empty
    AFTER ``.strip()`` — a raw whitespace ``name`` like ``" "`` would
    satisfy ``Field(..., min_length=1)`` because that constraint counts
    pre-strip characters, so we use a ``mode='before'`` validator
    explicitly.

    The ``email`` field is further constrained by an RFC-5321-ish shape
    regex (``^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$``) — enough to catch typos
    like ``not-an-email`` without pretending to validate addressing
    semantics.
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    email: str

    @field_validator("name", "email", mode="before")
    @classmethod
    def _non_empty_after_strip(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must be non-empty after .strip()")
        return value

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            raise ValueError("must match RFC-5321-ish shape '<local>@<domain>.<tld>'")
        return value


class OperatorConfig(BaseModel):
    """Top-level [operator] block carrying both attribution identities.

    Issue #347: ``[operator.supervisor]`` names the human who actively
    orchestrated this run; ``[operator.signer]`` names the human who
    legally attests DCO. Both required — the daemon refuses to boot
    without both, so every downstream commit pathway can rely on the
    resolver returning a real ``OperatorConfig`` with both fields
    populated ("make the right thing easy" — Google §).

    Supervisor and signer may legitimately resolve to the same identity
    (common single-operator case); the schema does not enforce
    uniqueness.
    """

    model_config = ConfigDict(extra="forbid")
    supervisor: OperatorIdentity
    signer: OperatorIdentity


class ProjectOperatorOverride(BaseModel):
    """Per-project [[projects.operator]] override (per-identity, optional).

    Issue #347 + the @wrenrichley 2026-06-19 comment: a project MAY
    override one identity, the other, both, or neither. Unset fields
    inherit from the top-level :class:`OperatorConfig`. Each override,
    when present, is validated with the same rules as a top-level
    identity.
    """

    model_config = ConfigDict(extra="forbid")
    supervisor: OperatorIdentity | None = None
    signer: OperatorIdentity | None = None


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    repo: str
    local_clone_path: str
    trigger_label: str = "foreman:plan"

    # Phase 8b.2: per-project fields the legacy run_<role> async function
    # bodies read off v3 ProjectConfig. v4 grows these so the role CLIs
    # (Phase 8b.3) can switch from v3 config to v4 config without
    # changing the legacy run_<role> signatures.
    check_command: str | None = None
    """Project's verification command. Worker runs this before claiming
    done. Resolves to ``"just check"`` when None. Matches v3
    ProjectConfig.check_command semantics."""

    dev_base_branch: str | None = None
    """Optional alternate branch to use as the base when creating spec
    worktrees, instead of origin/<default_branch>. Set when the project's
    active development line lives on a feature branch (e.g., during a
    walking-skeleton phase) rather than on main. The branch must exist
    on origin; Foreman will fetch it before branching."""

    max_fix_attempts: int = Field(default=3, ge=1)
    """Maximum Fixer cycles before the role escalates to NeedsHelp.
    Matches v3 ProjectConfig.max_fix_attempts default + ge=1 validation
    (a value of 0 would silently skip the cycle and dump to NeedsHelp
    on first dispatch). Read at role runtime by ``foreman.roles.fixer``."""

    max_impl_attempts: int = Field(default=3, ge=1)
    """Maximum Worker impl cycles before the role escalates to NeedsHelp.
    Matches v3 ProjectConfig.max_impl_attempts default + ge=1 validation
    (a value of 0 would silently skip the cycle and dump to NeedsHelp
    on first dispatch). Read at role runtime by ``foreman.roles.worker``."""

    operator: ProjectOperatorOverride | None = None
    """Per-project per-identity override of the top-level
    :class:`OperatorConfig`. Either, both, or neither of
    ``operator.supervisor`` / ``operator.signer`` may be set; unset
    fields inherit from the top-level identity via
    :func:`resolve_operator`. Issue #347."""

    auto_merge_impl: bool = False
    """foreman#418: the impl-merge gate.

    When ``False`` (the default), an approved impl PR does NOT
    auto-merge — the ticket parks at ``ImplApproved`` (a
    terminal-for-the-machine holding state) so a human can review and
    merge the PR themselves. When ``True``, foreman auto-merges the
    approved impl PR via ``MergingState`` — the historic behavior.

    Default-safe: an operator must explicitly opt in per project to
    re-enable autonomous impl merging. Spec PRs are unaffected by this
    flag — they keep auto-merging."""


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


class BackupConfig(BaseModel):
    """SQLite snapshot scheduler config (issue #360).

    Controls the daemon-internal scheduler that takes online
    ``sqlite3.Connection.backup()``-based snapshots of the live
    ``foreman.sqlite`` DB, writes them gzip-compressed to ``dir``, and
    enforces a tier-based retention policy (``retention_hourly``,
    ``retention_daily``, ``retention_weekly``).

    ``interval_seconds`` has a ``ge=60`` floor as runaway defense — a
    misconfigured ``0`` would snapshot every daemon tick (default 30s)
    and fill disk in minutes.

    Defaults reflect the issue body's stated retention goal: 24/7/4 →
    ~35 files total, each in the low-MB range after gzip → tens of MB
    on disk.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    dir: str = "/foreman/backups"
    interval_seconds: int = Field(default=3600, ge=60)
    retention_hourly: int = Field(default=24, ge=0)
    retention_daily: int = Field(default=7, ge=0)
    retention_weekly: int = Field(default=4, ge=0)


class StorageConfig(BaseModel):
    """Persistence engine selection. Defaulted so configs without a
    ``[storage]`` block keep the historical SQLite behavior.

    ``engine = "sqlite"`` (default) uses ``SqliteTicketRepository`` at
    ``V4Config.db_path``. ``engine = "postgres"`` uses
    ``PostgresTicketRepository`` at ``dsn`` with a thread-safe
    connection pool sized ``[pool_min, pool_max]``.
    """

    model_config = ConfigDict(extra="forbid")
    engine: Literal["sqlite", "postgres"] = "sqlite"
    dsn: str | None = None
    pool_min: int = Field(default=2, ge=1)
    pool_max: int = Field(default=10, ge=1)

    @model_validator(mode="after")
    def _dsn_required_for_postgres(self) -> StorageConfig:
        if self.engine == "postgres" and not self.dsn:
            raise ValueError('dsn is required when engine = "postgres"')
        if self.pool_max < self.pool_min:
            raise ValueError("pool_max must be >= pool_min")
        return self


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
    clone_refresh_seconds: int = Field(default=300, ge=0)
    """foreman#407: minimum wall-clock interval between per-poll
    ``origin/<default-branch>`` refreshes of each project's local clone.
    The daemon's clone refresher fetches a project's clone at most once
    per this many seconds, regardless of how frequently ``tick_seconds``
    fires. Default 300 (5 min): with the 30s default ``tick_seconds`` that
    refreshes a clone every ~10th tick — fresh enough that a base ref
    rarely goes more than a few minutes stale, without hammering
    ``git fetch`` (and the remote) on every poll. ge=0 because 0 means
    "refresh every tick" (the v3 per-poll behavior), which is a valid if
    aggressive choice; negative would be nonsense."""
    max_state_attempts: int = Field(default=3, ge=1)
    """Maximum consecutive same-state failures before the state machine
    forces a transition to NeedsHelp instead of running the role again.
    Default 3. Runaway defense — the 8b dogfood loop went 86 attempts
    in 43 minutes before this existed. Matches the shape of
    max_fix_attempts / max_impl_attempts but applies at state-machine
    granularity (per state per ticket) rather than role-level. ge=1
    because a value of 0 would dump every ticket to NeedsHelp on first
    entry — never a valid configuration."""
    apps: AppsConfig
    # Task 8.4: orchestrator is REQUIRED (no default). Google-style App
    # installation credentials are the only supported path — there is no
    # PAT-env-var fallback. A missing [orchestrator] block raises
    # ValidationError at load time, same as the per-role [apps.*] blocks.
    orchestrator: OrchestratorConfig
    operator: OperatorConfig
    """Issue #347: REQUIRED top-level operator-identity block carrying
    both :class:`OperatorIdentity` sub-tables (``supervisor`` + ``signer``)
    used to build the ``Supervised-by:`` and ``Signed-off-by:`` trailers
    on every role-bot commit. No default — the daemon refuses to boot
    without an operator block so every downstream commit path can rely
    on :func:`resolve_operator` returning both identities."""
    projects: list[ProjectConfig] = Field(default_factory=list)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    """Issue #360: periodic SQLite snapshot scheduler. Defaulted (NOT
    required) so configs without a ``[backup]`` block continue to load —
    backups default to ON with the documented hourly schedule + 24/7/4
    retention totalling ~35 files."""
    storage: StorageConfig = Field(default_factory=StorageConfig)
    """v5: persistence engine selection. Defaulted so pre-v5 configs
    (no [storage] block) keep loading with SQLite at db_path."""


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
    if "operator" in raw:
        payload["operator"] = raw["operator"]
    if "backup" in raw:
        payload["backup"] = raw["backup"]
    if "storage" in raw:
        payload["storage"] = raw["storage"]
    return V4Config.model_validate(payload)


def resolve_operator(project: ProjectConfig, config: V4Config) -> OperatorConfig:
    """Resolve the operator identities for ``project``.

    Issue #347: returns a fresh :class:`OperatorConfig` whose
    ``supervisor`` is ``project.operator.supervisor`` if set, else
    ``config.operator.supervisor``; ``signer`` is resolved the same way
    independently. Both fields are guaranteed populated because the
    top-level :class:`OperatorConfig` is required at config load.

    Pure function (no I/O). Single resolution surface every consumer
    (Planner / Worker / Fixer) calls so the project → top-level fallback
    isn't duplicated as ``project.operator.X or config.operator.X``
    ladders across the codebase.
    """
    override = project.operator
    supervisor = (
        override.supervisor
        if override is not None and override.supervisor is not None
        else config.operator.supervisor
    )
    signer = (
        override.signer
        if override is not None and override.signer is not None
        else config.operator.signer
    )
    return OperatorConfig(supervisor=supervisor, signer=signer)
