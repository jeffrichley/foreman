"""bootstrap_cli_context — turns V4Config into a CliContext.

Logging cleanup between tests is handled by the v4-scoped autouse
fixture in ``conftest.py`` (bootstrap_cli_context calls
configure_logging, which mutates process-global handler + propagate
state on the v4 loggers).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from foreman.v4.bootstrap import bootstrap_cli_context
from foreman.v4.config import (
    AppCredentials,
    AppsConfig,
    OperatorConfig,
    OperatorIdentity,
    OrchestratorConfig,
    ProjectConfig,
    SandboxConfig,
    StorageConfig,
    V4Config,
)
from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.observers.label_observability import LabelObservabilityObserver
from foreman.v4.observers.metrics import MetricsObserver
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.observers.sustained_blocked import SustainedBlockedObserver
from foreman.v4.observers.terminal_landing import TerminalLandingObserver
from foreman.v4.postgres_repository import PostgresTicketRepository
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.routing_git_provider import RoutingGitProvider
from foreman.v4.sandbox import SandboxUnavailableError

_TEST_DSN = "postgresql://u:p@localhost:5432/foreman"


def _storage_config() -> StorageConfig:
    """Postgres-only storage block for bootstrap wiring tests.

    StorageConfig is Postgres-only (kill-sqlite): every V4Config used in
    these tests must carry a valid postgres storage block with a dsn, or
    the ``StorageConfig`` validator refuses at construction. The
    ``_stub_postgres`` autouse fixture stubs ``from_dsn`` so no live DB is
    required — bootstrap gets a real (in-memory) repo object back."""
    return StorageConfig(engine="postgres", dsn=_TEST_DSN)


@pytest.fixture(autouse=True)
def _stub_postgres(monkeypatch):
    """Stub ``PostgresTicketRepository.from_dsn`` so bootstrap wiring
    tests build a real repo object without a live Postgres. Returns an
    :class:`InMemoryTicketRepository` — a real repo that satisfies every
    construction-time need (bootstrap never queries the repo while
    wiring)."""

    def fake_from_dsn(dsn, *, pool_min, pool_max):
        return InMemoryTicketRepository()

    monkeypatch.setattr(
        PostgresTicketRepository,
        "from_dsn",
        staticmethod(fake_from_dsn),
    )


def _stub_identity():
    mod = MagicMock()
    mod.get_role_token.return_value = "ghp_TOKEN"
    return mod


def _stub_git_factory():
    return MagicMock()


def _apps_config() -> AppsConfig:
    """Task 8.3: V4Config now requires ``apps``. These tests don't care
    about app identity — they exercise the bootstrap wiring — so a
    single fake-creds quadruple keeps the V4Config(...) calls compact."""
    creds = AppCredentials(app_id=12345, private_key_path="/tmp/fake.pem")
    return AppsConfig(planner=creds, reviewer=creds, fixer=creds, worker=creds)


def _orchestrator_config() -> OrchestratorConfig:
    """Task 8.4: V4Config now requires ``orchestrator`` too — the
    Google-style App installation pivot dropped the env-var PAT
    fallback. Same compact-fakes pattern as :func:`_apps_config`."""
    return OrchestratorConfig(
        app_id=99999,
        private_key_path="/tmp/fake-orchestrator.pem",
    )


def _operator_config() -> OperatorConfig:
    """Issue #347: V4Config now requires a top-level [operator] block.
    Compact-fakes pattern for tests that don't exercise operator wiring."""
    return OperatorConfig(
        supervisor=OperatorIdentity(name="Test Supervisor", email="sup@example.com"),
        signer=OperatorIdentity(name="Test Signer", email="sign@example.com"),
    )


def test_bootstrap_returns_clicontext_with_all_fields(tmp_path: Path):
    config = V4Config(
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        storage=_storage_config(),
        projects=[
            ProjectConfig(
                name="voice",
                repo="owner/voice",
                local_clone_path=str(tmp_path / "voice"),
            ),
        ],
    )
    ctx = bootstrap_cli_context(
        config=config,
        identity=_stub_identity(),
        git_provider_factory=lambda repo: _stub_git_factory(),
    )
    assert ctx.repo is not None
    assert ctx.qm is not None
    assert ctx.daemon is not None
    assert ctx.dispatcher is not None


def test_bootstrap_builds_one_poller_per_project(tmp_path: Path):
    config = V4Config(
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        storage=_storage_config(),
        projects=[
            ProjectConfig(name="a", repo="o/a", local_clone_path=str(tmp_path / "a")),
            ProjectConfig(name="b", repo="o/b", local_clone_path=str(tmp_path / "b")),
            ProjectConfig(name="c", repo="o/c", local_clone_path=str(tmp_path / "c")),
        ],
    )
    ctx = bootstrap_cli_context(
        config=config,
        identity=_stub_identity(),
        git_provider_factory=lambda repo: _stub_git_factory(),
    )
    assert len(ctx.daemon._pollers) == 3  # type: ignore[attr-defined]


def test_bootstrap_wires_event_bus_with_standard_observers(tmp_path: Path):
    """Task 8.1: bootstrap constructs the EventBus + subscribes the four
    standard observers + threads the bus into Daemon at construction.

    Asserts:
      - ``ctx.daemon._bus`` is not None (bus instance is reachable).
      - exactly four observers are subscribed.
      - one of each of the standard observer types is present.
    """
    config = V4Config(
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        storage=_storage_config(),
        projects=[
            ProjectConfig(
                name="voice",
                repo="owner/voice",
                local_clone_path=str(tmp_path / "voice"),
            ),
        ],
    )
    ctx = bootstrap_cli_context(
        config=config,
        identity=_stub_identity(),
        # Real FakeGitProvider — satisfies the LabelWriter Protocol that
        # LabelObservabilityObserver needs at construction time.
        git_provider_factory=lambda repo: FakeGitProvider(),
    )
    assert ctx.daemon is not None
    bus = ctx.daemon._bus  # type: ignore[attr-defined]
    assert bus is not None, "bootstrap should have constructed an EventBus"

    subscribers = bus._subscribers  # type: ignore[attr-defined]
    # foreman#367: bootstrap now wires SustainedBlockedObserver and
    # TerminalLandingObserver in addition to the original four
    # observers, bringing the total to 6.
    assert len(subscribers) == 6, (
        f"expected 6 observers (structured-log, event-archive, "
        f"label-observability, metrics, sustained-blocked, "
        f"terminal-landing), got {len(subscribers)}"
    )

    observer_types = {type(s) for s in subscribers}
    assert StructuredLogObserver in observer_types
    assert EventArchiveObserver in observer_types
    assert LabelObservabilityObserver in observer_types
    assert MetricsObserver in observer_types
    assert SustainedBlockedObserver in observer_types
    assert TerminalLandingObserver in observer_types


def test_bootstrap_wires_routing_git_provider_for_multi_project(tmp_path: Path):
    """Phase 8d.16 F1 fix: bootstrap must wrap the per-project providers
    in a :class:`RoutingGitProvider` for cross-project consumers (the
    Daemon's state machine, the LabelObservabilityObserver). Before the
    fix, only the first project's provider was threaded into both — so
    every other project's writes silently hit project[0]'s repo.

    Asserts the Daemon's ``git`` attribute is a ``RoutingGitProvider``
    AND that its ``_providers`` map carries one entry per configured
    project. Per-project Pollers keep their own GitProvider directly
    (one project per Poller, no routing hop needed) — verified by
    checking each Poller's ``_git`` is NOT the router."""
    voice_provider = FakeGitProvider()
    foreman_provider = FakeGitProvider()
    by_repo = {"owner/voice": voice_provider, "owner/foreman": foreman_provider}

    config = V4Config(
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        storage=_storage_config(),
        projects=[
            ProjectConfig(
                name="voice",
                repo="owner/voice",
                local_clone_path=str(tmp_path / "voice"),
            ),
            ProjectConfig(
                name="foreman",
                repo="owner/foreman",
                local_clone_path=str(tmp_path / "foreman"),
            ),
        ],
    )
    ctx = bootstrap_cli_context(
        config=config,
        identity=_stub_identity(),
        git_provider_factory=lambda repo: by_repo[repo],
    )
    assert ctx.daemon is not None

    # Daemon receives the router for cross-project calls.
    daemon_git = ctx.daemon._git  # type: ignore[attr-defined]
    assert isinstance(daemon_git, RoutingGitProvider), (
        f"Daemon.git should be a RoutingGitProvider for multi-project "
        f"routing; got {type(daemon_git).__name__}. F1 not fixed."
    )

    # And the router's per-project map covers EVERY configured project,
    # keyed by ProjectConfig.name (not repo full name).
    routed_projects = set(daemon_git._providers)  # type: ignore[attr-defined]
    assert routed_projects == {"voice", "foreman"}, (
        f"router should know every project; got {routed_projects}"
    )

    # And each Poller still uses its OWN per-project provider directly,
    # not the router (pollers operate on one project each — routing hop
    # would be pure overhead).
    pollers = ctx.daemon._pollers  # type: ignore[attr-defined]
    for poller in pollers:
        assert not isinstance(poller._git, RoutingGitProvider), (
            f"Poller for {poller._project} should hold its own per-project "
            f"GitProvider, not the router."
        )


def test_bootstrap_skips_label_observer_when_no_projects(tmp_path: Path):
    """Zero-project configs have no GitProvider to construct a
    LabelObservabilityObserver from. The other three observers still
    land on the bus so the surface remains observable."""
    config = V4Config(
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        storage=_storage_config(),
        projects=[],
    )
    ctx = bootstrap_cli_context(
        config=config,
        identity=_stub_identity(),
        git_provider_factory=lambda repo: FakeGitProvider(),
    )
    assert ctx.daemon is not None
    bus = ctx.daemon._bus  # type: ignore[attr-defined]
    assert bus is not None
    observer_types = {type(s) for s in bus._subscribers}  # type: ignore[attr-defined]
    assert LabelObservabilityObserver not in observer_types
    assert StructuredLogObserver in observer_types
    assert EventArchiveObserver in observer_types
    assert MetricsObserver in observer_types


def test_bootstrap_uses_postgres_when_engine_postgres(monkeypatch, tmp_path: Path):
    """Task 11: with ``storage.engine == "postgres"``, bootstrap selects
    :class:`PostgresTicketRepository` via ``from_dsn`` (no live DB — the
    classmethod is stubbed) and threads the dsn + pool sizes through. The
    file-snapshot backup is SQLite-specific, so under Postgres the daemon
    gets the disabled-backup sentinel instead of a real scheduler."""
    captured: dict[str, object] = {}

    def fake_from_dsn(dsn, *, pool_min, pool_max):
        captured["dsn"] = dsn
        captured["pool_min"] = pool_min
        captured["pool_max"] = pool_max
        # An InMemoryTicketRepository is a real repo object, which is all
        # bootstrap needs at construction time (it never calls the repo
        # during wiring, and never touches the postgres-absent
        # ``.connection`` attribute on the postgres branch).
        return InMemoryTicketRepository()

    monkeypatch.setattr(
        PostgresTicketRepository,
        "from_dsn",
        staticmethod(fake_from_dsn),
    )

    config = V4Config(
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        storage=StorageConfig(
            engine="postgres",
            dsn="postgresql://u:p@localhost:5432/foreman",
            pool_min=3,
            pool_max=7,
        ),
        projects=[
            ProjectConfig(
                name="voice",
                repo="owner/voice",
                local_clone_path=str(tmp_path / "voice"),
            ),
        ],
    )
    ctx = bootstrap_cli_context(
        config=config,
        identity=_stub_identity(),
        git_provider_factory=lambda repo: FakeGitProvider(),
    )

    # from_dsn was called with the configured dsn + pool sizes.
    assert captured["dsn"] == "postgresql://u:p@localhost:5432/foreman"
    assert captured["pool_min"] == 3
    assert captured["pool_max"] == 7
    assert isinstance(ctx.repo, InMemoryTicketRepository)

    # foreman#434: bootstrap wires a real BackupScheduler (enabled=True by
    # default) into the daemon when the Postgres DSN is available. The
    # SQLite file-snapshot scheduler was removed; this Postgres-aware
    # scheduler is the replacement.
    assert ctx.daemon is not None
    from foreman.v4.pg_backup import BackupScheduler

    assert isinstance(ctx.daemon._backup_scheduler, BackupScheduler)


# ----------------------------------------------------------------------
# foreman#476 — startup auto-clone loop
#
# bootstrap_cli_context now calls ensure_clone for each project whose
# local_clone_path doesn't exist (or exists but has no .git). The
# autouse fixture below stubs ensure_clone so the existing tests are
# not affected by the new startup step (the paths they pass don't exist
# on disk and would trigger a real clone attempt without the stub).
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_ensure_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``foreman.v4.bootstrap.ensure_clone`` to a no-op MagicMock so
    existing bootstrap tests are not broken by the startup clone loop added
    in foreman#476. Tests that need to assert clone behaviour override this
    fixture with their own ``monkeypatch.setattr`` call."""
    monkeypatch.setattr("foreman.v4.bootstrap.ensure_clone", MagicMock())


def test_bootstrap_clones_missing_project_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_clone is called at bootstrap for each project whose
    local_clone_path doesn't exist on disk, using the orchestrator token
    and the full HTTPS GitHub URL (foreman#476).

    Production always ships config.projects=[] (issue #477 template change)
    and passes projects via the ``projects=`` kwarg. The test mirrors that
    real call path so the assert exercises the ``active_projects`` branch,
    not the dead ``config.projects`` branch."""
    mock_ensure_clone = MagicMock()
    monkeypatch.setattr("foreman.v4.bootstrap.ensure_clone", mock_ensure_clone)

    pc = ProjectConfig(
        name="voice",
        repo="owner/voice",
        local_clone_path=str(tmp_path / "voice"),
    )
    # config.projects is EMPTY — mirrors production after #503.
    config = V4Config(
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        storage=_storage_config(),
        projects=[],
    )
    identity = _stub_identity()
    bootstrap_cli_context(
        config=config,
        identity=identity,
        git_provider_factory=lambda repo: _stub_git_factory(),
        # Project supplied via kwarg — the production call path.
        projects=[pc],
    )

    mock_ensure_clone.assert_called_once_with(
        repo_url="https://github.com/owner/voice.git",
        clone_path=Path(str(tmp_path / "voice")),
        token="ghp_TOKEN",
    )


def test_bootstrap_skips_clone_for_existing_valid_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_clone is NOT called when local_clone_path already contains a
    .git directory — the startup loop's idempotency fast-path fires and the
    orchestrator token is never minted (foreman#476).

    Uses the production call path: config.projects=[], project via ``projects=``
    kwarg (mirrors issue #477 / #503 template change)."""
    # Simulate an existing valid clone by creating the .git directory.
    git_dir = tmp_path / "voice" / ".git"
    git_dir.mkdir(parents=True)

    mock_ensure_clone = MagicMock()
    monkeypatch.setattr("foreman.v4.bootstrap.ensure_clone", mock_ensure_clone)

    pc = ProjectConfig(
        name="voice",
        repo="owner/voice",
        local_clone_path=str(tmp_path / "voice"),
    )
    # config.projects is EMPTY — mirrors production after #503.
    config = V4Config(
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        storage=_storage_config(),
        projects=[],
    )
    bootstrap_cli_context(
        config=config,
        identity=_stub_identity(),
        git_provider_factory=lambda repo: _stub_git_factory(),
        # Project supplied via kwarg — the production call path.
        projects=[pc],
    )

    mock_ensure_clone.assert_not_called()


def test_bootstrap_survives_transient_clone_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CalledProcessError from ensure_clone is caught and logged; the daemon
    still starts (bootstrap_cli_context returns normally) and subsequent
    projects in the list are still processed (foreman#476 graceful-degrade).

    Scenario: two projects, the first clone fails transiently, the second
    succeeds. bootstrap_cli_context must:
    - NOT raise
    - call ensure_clone for BOTH projects
    - still return a valid CliContext (daemon can start)."""
    call_order: list[str] = []

    def fake_ensure_clone(*, repo_url: str, clone_path: Path, token: str) -> None:
        call_order.append(repo_url)
        if "owner/failing" in repo_url:
            raise subprocess.CalledProcessError(128, ["git", "clone"])
        # second project succeeds — no-op is fine

    monkeypatch.setattr("foreman.v4.bootstrap.ensure_clone", fake_ensure_clone)

    pc_fail = ProjectConfig(
        name="failing",
        repo="owner/failing",
        local_clone_path=str(tmp_path / "failing"),
    )
    pc_ok = ProjectConfig(
        name="ok",
        repo="owner/ok",
        local_clone_path=str(tmp_path / "ok"),
    )
    # config.projects is EMPTY — mirrors production after #503.
    config = V4Config(
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        storage=_storage_config(),
        projects=[],
    )
    # Must not raise — daemon starts despite the transient clone failure.
    # The token-scrub subprocess.run is guarded by (clone_path / ".git").exists(),
    # which is False here (fake_ensure_clone creates no on-disk artifacts).
    ctx = bootstrap_cli_context(
        config=config,
        identity=_stub_identity(),
        git_provider_factory=lambda repo: _stub_git_factory(),
        projects=[pc_fail, pc_ok],
    )

    # Daemon still starts — a valid CliContext is returned.
    assert ctx is not None
    assert ctx.daemon is not None

    # Both projects were attempted — the failure did not short-circuit.
    assert "https://github.com/owner/failing.git" in call_order
    assert "https://github.com/owner/ok.git" in call_order


def test_skips_clone_loop_when_run_startup_clone_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_startup_clone=False must not mint the orchestrator token.

    A sandboxed role subprocess's private clone is already prepped by the
    daemon; it has no PEM of its own to mint an orchestrator token with,
    so the daemon-level all-projects clone loop must be fully skippable.
    Uses a raising identity — if the clone loop ran at all, it would call
    ``get_role_token("orchestrator")`` and fail the test immediately."""

    class _RaisingIdentity:
        def get_role_token(self, role: str) -> str:
            raise AssertionError(f"clone loop ran: minted token for {role!r}")

    pc = ProjectConfig(
        name="voice",
        repo="owner/voice",
        local_clone_path=str(tmp_path / "voice"),
    )
    # config.projects is EMPTY — mirrors production after #503.
    config = V4Config(
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        storage=_storage_config(),
        projects=[],
    )
    ctx = bootstrap_cli_context(
        config=config,
        identity=_RaisingIdentity(),
        git_provider_factory=lambda repo: _stub_git_factory(),
        # Project supplied via kwarg — the production call path — and
        # non-empty on purpose, so the only thing preventing the clone
        # loop from running is run_startup_clone=False.
        projects=[pc],
        run_startup_clone=False,
    )
    assert ctx is not None


# ----------------------------------------------------------------------
# foreman#job-sandbox-isolation Task 6 — bootstrap wiring
#
# bootstrap_cli_context builds a SandboxLauncher from config.sandbox and
# runs preflight() at startup when sandbox.enabled. A preflight failure
# is fail-closed (raises SandboxUnavailableError) unless
# allow_unsandboxed is set, in which case it logs a loud warning and
# continues unsandboxed. sandbox.enabled=False skips preflight entirely
# and builds the dispatcher exactly as before (launcher=None).
# ----------------------------------------------------------------------


@pytest.fixture
def fake_identity():
    """A stub IdentityProvider — mirrors ``_stub_identity()`` above."""
    return _stub_identity()


@pytest.fixture
def fake_git_factory():
    """A stub git-provider factory — mirrors ``_stub_git_factory()`` above."""
    return lambda repo: _stub_git_factory()


@pytest.fixture
def minimal_config_with_sandbox(tmp_path: Path):
    """Factory fixture: a minimal V4Config with ``[sandbox]`` set.

    Reuses the module's existing compact-fakes helpers for the other
    required blocks (``apps`` / ``orchestrator`` / ``operator`` /
    ``storage``), then overrides ``sandbox`` via ``model_copy`` per the
    Task 6 brief. ``projects`` defaults to empty — most sandbox-wiring
    tests don't care about project config — but foreman#556 callers pass
    a project list to exercise ``sandbox_projects`` map construction.
    """

    def _make(
        *,
        enabled: bool,
        allow_unsandboxed: bool,
        projects: list[ProjectConfig] | None = None,
    ) -> V4Config:
        base = V4Config(
            log_dir=str(tmp_path / "logs"),
            apps=_apps_config(),
            orchestrator=_orchestrator_config(),
            operator=_operator_config(),
            storage=_storage_config(),
            projects=projects or [],
        )
        return base.model_copy(
            update={
                "sandbox": SandboxConfig(
                    enabled=enabled,
                    allow_unsandboxed=allow_unsandboxed,
                )
            }
        )

    return _make


def test_bootstrap_fails_closed_when_preflight_fails(
    minimal_config_with_sandbox, fake_identity, fake_git_factory, monkeypatch
):
    """sandbox.enabled + preflight failure + allow_unsandboxed False => raise."""
    from foreman.v4 import bootstrap

    cfg = minimal_config_with_sandbox(enabled=True, allow_unsandboxed=False)

    def boom(**kwargs):
        raise SandboxUnavailableError("no userns")

    monkeypatch.setattr(bootstrap, "preflight", boom)
    with pytest.raises(SandboxUnavailableError):
        bootstrap.bootstrap_cli_context(
            config=cfg,
            identity=fake_identity,
            git_provider_factory=fake_git_factory,
        )


def test_bootstrap_warns_and_continues_when_allow_unsandboxed(
    minimal_config_with_sandbox, fake_identity, fake_git_factory, monkeypatch, caplog
):
    """sandbox.enabled + preflight failure + allow_unsandboxed True => loud
    warning, no raise, dispatcher built unsandboxed.

    ``bootstrap_cli_context`` calls ``configure_logging`` as its first
    statement, which sets ``propagate=False`` on the ``foreman.v4``
    logger (see the module docstring at the top of this file + the
    ``_reset_v4_logging`` fixture in ``conftest.py``). ``bootstrap.py``'s
    own logger (``foreman.v4.bootstrap``) is a child of that logger, so
    once ``configure_logging`` has run, its records stop propagating at
    ``foreman.v4`` and never reach caplog's root-attached handler —
    ``caplog.set_level`` alone only adjusts levels, it does not attach a
    handler, so it cannot work around this. Attaching ``caplog.handler``
    directly to the bootstrap logger sidesteps the severed propagate
    chain; ``_reset_v4_logging`` tears the logger's handlers down again
    at teardown, so this doesn't leak into other tests."""
    import logging

    from foreman.v4 import bootstrap

    cfg = minimal_config_with_sandbox(enabled=True, allow_unsandboxed=True)

    def boom(**kwargs):
        raise SandboxUnavailableError("no userns")

    monkeypatch.setattr(bootstrap, "preflight", boom)
    logging.getLogger("foreman.v4.bootstrap").addHandler(caplog.handler)
    ctx = bootstrap.bootstrap_cli_context(
        config=cfg,
        identity=fake_identity,
        git_provider_factory=fake_git_factory,
    )
    assert ctx is not None
    assert any("unsandboxed" in r.message.lower() for r in caplog.records)
    dispatcher = ctx.dispatcher
    assert dispatcher._sandbox is None  # type: ignore[attr-defined]


def test_bootstrap_disabled_sandbox_skips_preflight(
    minimal_config_with_sandbox, fake_identity, fake_git_factory, monkeypatch
):
    """sandbox.enabled False => preflight never runs, dispatcher unsandboxed."""
    from foreman.v4 import bootstrap

    cfg = minimal_config_with_sandbox(enabled=False, allow_unsandboxed=False)

    def boom(**kwargs):
        raise AssertionError("preflight must not run when sandbox disabled")

    monkeypatch.setattr(bootstrap, "preflight", boom)
    ctx = bootstrap.bootstrap_cli_context(
        config=cfg,
        identity=fake_identity,
        git_provider_factory=fake_git_factory,
    )
    assert ctx is not None
    dispatcher = ctx.dispatcher
    assert dispatcher._sandbox is None  # type: ignore[attr-defined]
    # foreman#556: sandbox off => no project map either. Dispatch never
    # consults it (self._sandbox is None short-circuits the check in
    # SubprocessRoleDispatcher.dispatch), but asserting None here pins
    # the off-path as genuinely unchanged rather than incidentally safe.
    assert dispatcher._sandbox_projects is None  # type: ignore[attr-defined]


def test_bootstrap_threads_project_map_into_dispatcher_when_sandbox_enabled(
    minimal_config_with_sandbox, fake_identity, fake_git_factory, monkeypatch, tmp_path: Path
):
    """foreman#556: when sandbox.enabled, the dispatcher gets the project
    map so it can prep each job's private clone.

    The map is built unconditionally inside the ``sandbox.enabled``
    branch — not gated on whether preflight actually succeeds — so the
    ``allow_unsandboxed`` local-dev downgrade (which leaves
    ``sandbox_launcher`` / dispatcher's ``_sandbox`` at None) never
    leaves ``_sandbox_projects`` None while sandbox.enabled is True.
    Preflight is monkeypatched to fail deterministically (mirroring
    ``test_bootstrap_warns_and_continues_when_allow_unsandboxed``) so
    this doesn't depend on the test host's userns support.
    """
    from foreman.v4 import bootstrap

    cfg = minimal_config_with_sandbox(
        enabled=True,
        allow_unsandboxed=True,
        projects=[
            ProjectConfig(
                name="foreman",
                repo="owner/foreman",
                local_clone_path=str(tmp_path / "foreman-clone"),
            ),
        ],
    )

    def boom(**kwargs):
        raise SandboxUnavailableError("no userns")

    monkeypatch.setattr(bootstrap, "preflight", boom)
    ctx = bootstrap.bootstrap_cli_context(
        config=cfg,
        identity=fake_identity,
        git_provider_factory=fake_git_factory,
    )
    dispatcher = ctx.dispatcher
    # The preflight failure + allow_unsandboxed downgrades the launcher
    # to None (mirrors test_bootstrap_warns_and_continues_when_allow_unsandboxed)
    # — confirms the map is populated on this branch specifically, not
    # merely on the preflight-succeeds happy path.
    assert dispatcher._sandbox is None  # type: ignore[attr-defined]
    sandbox_projects = dispatcher._sandbox_projects  # type: ignore[attr-defined]
    assert sandbox_projects is not None
    assert "foreman" in sandbox_projects
    project_cfg = sandbox_projects["foreman"]
    assert project_cfg.repo == "owner/foreman"
    assert project_cfg.local_clone_path == str(tmp_path / "foreman-clone")


# ----------------------------------------------------------------------
# foreman#role-identity hardening — skip the daemon sandbox setup block
# entirely when bootstrap runs INSIDE a sandbox box.
#
# A boxed role subprocess runs the full main() -> bootstrap_cli_context
# with the same in-box config the daemon loaded, so config.sandbox.enabled
# is still True. Without a second guard, bootstrap would run a NESTED
# preflight() (bwrap-boot-inside-the-bwrap-box) and build a SandboxLauncher
# + bot_metadata the boxed role never uses — and a fail-closed nested
# preflight failure would crash the box before the role runs. The box sets
# FOREMAN_SANDBOXED=1 in its own environment (see sandbox.py), which is the
# signal bootstrap_cli_context now checks.
# ----------------------------------------------------------------------


def test_bootstrap_skips_preflight_when_already_in_sandbox_box(
    minimal_config_with_sandbox, fake_identity, fake_git_factory, monkeypatch
):
    """sandbox.enabled=True + FOREMAN_SANDBOXED=1 => preflight must NOT run
    and the dispatcher must come up fully unsandboxed (sandbox=None,
    bot_metadata=None). This is the boxed-role path: the box is already
    sandboxed, so bootstrap must not attempt a nested preflight or build
    daemon sandbox machinery it will never use."""
    from foreman.v4 import bootstrap

    monkeypatch.setenv("FOREMAN_SANDBOXED", "1")
    cfg = minimal_config_with_sandbox(enabled=True, allow_unsandboxed=False)

    def boom(**kwargs):
        raise AssertionError("preflight must not run when already inside a sandbox box")

    monkeypatch.setattr(bootstrap, "preflight", boom)
    ctx = bootstrap.bootstrap_cli_context(
        config=cfg,
        identity=fake_identity,
        git_provider_factory=fake_git_factory,
    )
    assert ctx is not None
    dispatcher = ctx.dispatcher
    assert dispatcher._sandbox is None  # type: ignore[attr-defined]
    assert dispatcher._bot_metadata is None  # type: ignore[attr-defined]
    assert dispatcher._sandbox_projects is None  # type: ignore[attr-defined]


def test_bootstrap_daemon_still_sandboxes_when_not_in_box(
    minimal_config_with_sandbox, fake_identity, fake_git_factory, monkeypatch
):
    """Control case for the FOREMAN_SANDBOXED gate above: sandbox.enabled=True
    with FOREMAN_SANDBOXED unset (the daemon's own process, not a boxed role)
    must still run preflight and build a real SandboxLauncher — proving the
    new guard only skips setup when actually running in-box, not whenever
    sandbox.enabled is True."""
    from foreman.v4 import bootstrap

    monkeypatch.delenv("FOREMAN_SANDBOXED", raising=False)
    cfg = minimal_config_with_sandbox(enabled=True, allow_unsandboxed=False)

    preflight_calls: list[dict[str, object]] = []

    def fake_preflight(**kwargs):
        preflight_calls.append(kwargs)

    monkeypatch.setattr(bootstrap, "preflight", fake_preflight)
    ctx = bootstrap.bootstrap_cli_context(
        config=cfg,
        identity=fake_identity,
        git_provider_factory=fake_git_factory,
    )
    assert len(preflight_calls) == 1
    dispatcher = ctx.dispatcher
    assert dispatcher._sandbox is not None  # type: ignore[attr-defined]
