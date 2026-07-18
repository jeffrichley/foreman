"""V4Config — TOML-loaded daemon configuration.

This IS a pydantic model: config is a boundary surface. TOML on disk
is untrusted input the daemon parses at startup; pydantic's validation
catches typos, wrong types, invalid enum values before the daemon
half-starts and confuses everything downstream.

Schema:
  [daemon]
    log_dir              - directory for rich-stdout + jsonl
    log_level            - default INFO
    tick_seconds         - cadence between Poller ticks (default 30)
    max_in_flight        - GLOBAL concurrency cap: total tickets in
                           flight across ALL repos. Sizes BOTH the QM
                           in-flight cap AND the WorkerPool
                           ThreadPoolExecutor. Default 1. Values > 1
                           parallelise DIFFERENT repos; each repo stays
                           serial by default via the per-repo
                           ProjectConfig.max_in_flight cap (default 1,
                           opt-in above), which holds the same-repo
                           merge-race invariant. See that field for the
                           full why.
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
    max_in_flight     - per-REPO concurrency cap (default 1, ge=1; opt-in
                        above 1). Was pinned to 1 (le=1, hardened
                        2026-07-17) for the same-repo merge-race
                        invariant; foreman#550's merge coordinator now
                        serializes merges per repo, so > 1 is safe.
                        Issue #472; lifted by foreman#550.
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
    r"""One human operator identity (name + email) used in a commit trailer.

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
    """One ``[[projects]]`` TOML entry: a single repo the daemon manages.

    See the module docstring's ``[[projects]]`` schema section for the
    full field-by-field description; several fields below also carry
    their own attribute docstrings for the finer per-field rationale.
    """

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

    max_in_flight: int = Field(default=1, ge=1)
    """Per-repo concurrency cap (issue #472; default 1, opt-in above that).

    Was pinned to exactly 1 (``le=1``, hardened by the self-heal
    arch-review, 2026-07-17): at most one in-flight ticket per repo,
    because a second concurrent ticket in the SAME repo could merge and
    leave this repo's other PR BEHIND its base mid-flight, the rebase
    race ``states/merging.py`` sidesteps (foreman#316).

    That ceiling is now lifted (foreman#550): the merge coordinator
    serializes merges per repo, so > 1 in-flight tickets in the same
    repo no longer race on the same-repo merge invariant. The default
    stays 1 — operators opt in to higher per-repo concurrency
    explicitly. The GLOBAL :attr:`V4Config.max_in_flight` cap is
    unaffected by this change; it still sizes total tickets across ALL
    repos. The QueueManager reads this as the per-project cap
    (``daemon.py`` builds ``project_caps`` from it)."""

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


class StorageConfig(BaseModel):
    """Persistence engine selection. Postgres-only.

    ``engine = "postgres"`` (the only supported value, and the default)
    uses ``PostgresTicketRepository`` at ``dsn`` with a thread-safe
    connection pool sized ``[pool_min, pool_max]``. ``dsn`` is required:
    constructing a ``StorageConfig`` without one raises a
    ``ValidationError`` (Jeff's "always loud fail" directive — a
    misconfigured storage block refuses at validation time rather than
    silently falling back).
    """

    model_config = ConfigDict(extra="forbid")
    engine: Literal["postgres"] = "postgres"
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


class BackupConfig(BaseModel):
    """Postgres pg_dump snapshot scheduler config (foreman#434).

    ``enabled`` turns the scheduler on/off. ``dir`` is the
    container-internal path where ``.sql.gz`` dumps are written
    (bind-mounted to ``~/.foreman/backups/`` on the host so
    ``docker compose down -v`` cannot wipe them). ``interval_seconds``
    controls how often the daemon fires a snapshot; ``ge=60`` guards
    against runaway snapshot spam if misconfigured to 0.

    Retention tiers:
      - ``retention_hourly`` — keep the N most-recent files in the
        last 24 h window (default 24).
      - ``retention_daily``  — keep the N most-recent calendar-day
        survivors from the ``[now-7d, now-24h)`` window (default 7).
      - ``retention_weekly`` — keep the N most-recent ISO-week
        survivors from the ``[now-28d, now-7d)`` window (default 4).
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    dir: str = "/foreman/backups"
    interval_seconds: int = Field(default=3600, ge=60)
    retention_hourly: int = Field(default=24, ge=0)
    retention_daily: int = Field(default=7, ge=0)
    retention_weekly: int = Field(default=4, ge=0)


class V4Config(BaseModel):
    """Root of the daemon's TOML-loaded configuration.

    See the module docstring for the complete schema of every section
    (``[daemon]``, ``[apps.*]``, ``[orchestrator]``, ``[operator]``,
    ``[[projects]]``, ``[storage]``, ``[backup]``). :func:`load_config`
    parses TOML into a payload dict and validates it against this model.
    """

    model_config = ConfigDict(extra="forbid")
    log_dir: str
    log_level: str = "INFO"
    tick_seconds: float = 30.0
    max_in_flight: int = Field(default=1, ge=1)
    """Global concurrency cap: the maximum number of tickets in flight
    across ALL repos at once (sizes the WorkerPool's ThreadPoolExecutor).

    This is a total-across-repos ceiling, NOT a per-repo one. The
    same-repo safety invariant — at most one in-flight ticket per repo,
    which sidesteps the concurrent-merge rebase race
    (``states/merging.py``, foreman#316) — is enforced separately by the
    per-repo :attr:`ProjectConfig.max_in_flight` cap (pinned to 1). So a
    value > 1 here safely parallelises DIFFERENT repos (e.g. one foreman
    ticket + one agent_core ticket at the same time) while each repo
    stays serial. ge=1 because 0 would size the executor at zero workers
    — no ticket would ever run. The old global ``== 1`` pin (the first
    foreman#316 guard) is lifted: the guard now lives at the per-repo
    grain, where the merge race actually is."""
    role_timeout_seconds: int = 600
    role_inactivity_timeout_seconds: int = Field(default=300, ge=0)
    """foreman#483: stdout/stderr inactivity watchdog for role subprocesses.

    A healthy role streams output continuously (tool calls, log lines); a
    hung one — the intermittent post-startup stall where the first agent
    turn blocks on I/O and emits nothing — is detectable in minutes. When
    a dispatched role produces no output on either stream for this many
    seconds, the dispatcher kills and re-dispatches it rather than waiting
    out the full ``role_timeout_seconds`` wall-clock ceiling (which only
    lengthens each silent burn before the retry that actually recovers).
    ``role_timeout_seconds`` remains the absolute backstop. Default 300
    (5 min). Set 0 to disable the watchdog and fall back to the flat
    wall-clock timeout alone."""
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
    storage: StorageConfig = Field(default_factory=StorageConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    """foreman#434: pg_dump snapshot scheduler. Optional with default
    — existing operator configs without a ``[backup]`` block continue
    to load and default to ``enabled=True`` with hourly snapshots."""
    """v5: persistence engine selection. Postgres-only with a required
    ``dsn`` — a config without a valid ``[storage]`` block raises a
    ``ValidationError`` at load time (loud-fail; no SQLite fallback)."""


class ProjectRegistry:
    """Atomic-rebind holder for the live project-config map.

    Shared by the Daemon (writer) and WorkerPool (reader) so that a
    hot-reload swap is a single reference assignment — not an in-place
    mutation of a shared dict. The GIL makes that single assignment
    atomic in CPython, so readers always see either the old dict or the
    new one; never a half-mutated intermediate.

    Usage::

        # Daemon init
        registry = ProjectRegistry({pc.name: pc for pc in projects})

        # Hot-reload (Daemon._apply_project_reload)
        registry.current = {pc.name: pc for pc in new_projects}

        # Reader (StateContext / WorkerPool)
        pc = registry.current.get(ticket.project)
    """

    def __init__(self, initial: dict[str, ProjectConfig]) -> None:
        # ``current`` is a plain dict attribute; assignment is atomic
        # under the GIL. No lock needed for reads.
        self.current: dict[str, ProjectConfig] = initial


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
    if "storage" in raw:
        payload["storage"] = raw["storage"]
    if "backup" in raw:
        payload["backup"] = raw["backup"]
    return V4Config.model_validate(payload)


def load_projects(path: Path) -> list[ProjectConfig]:
    """Parse a standalone ``[[projects]]`` TOML file and return validated entries.

    The file must contain only ``[[projects]]`` array-of-tables; daemon
    secrets and identity live in the envsubst-rendered config.toml (which
    ships with zero ``[[projects]]`` tables after issue #477).

    Raises:
        FileNotFoundError: propagated from ``Path.read_text`` when ``path``
            does not exist. Operator must create the file before starting
            the daemon (see ``projects.toml.example`` in the image).
        pydantic.ValidationError: on missing required fields or extra keys
            (same loud-fail contract as :func:`load_config`).
        tomllib.TOMLDecodeError: when the TOML file contains a syntax error
            (e.g. a malformed table header). Callers should catch this
            alongside ``FileNotFoundError`` and ``ValidationError``.
    """
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return [ProjectConfig.model_validate(entry) for entry in raw.get("projects", [])]


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
