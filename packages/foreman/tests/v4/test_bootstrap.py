"""bootstrap_cli_context — turns V4Config into a CliContext.

Logging cleanup between tests is handled by the v4-scoped autouse
fixture in ``conftest.py`` (bootstrap_cli_context calls
configure_logging, which mutates process-global handler + propagate
state on the v4 loggers).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from foreman.v4.bootstrap import bootstrap_cli_context
from foreman.v4.config import (
    AppCredentials,
    AppsConfig,
    OperatorConfig,
    OperatorIdentity,
    OrchestratorConfig,
    ProjectConfig,
    V4Config,
)
from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.observers.label_observability import LabelObservabilityObserver
from foreman.v4.observers.metrics import MetricsObserver
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.observers.sustained_blocked import SustainedBlockedObserver
from foreman.v4.observers.terminal_landing import TerminalLandingObserver
from foreman.v4.routing_git_provider import RoutingGitProvider


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
        app_id=99999, private_key_path="/tmp/fake-orchestrator.pem",
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
        db_path=str(tmp_path / "foreman.db"),
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        projects=[
            ProjectConfig(
                name="voice", repo="owner/voice",
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


def test_db_file_created_at_configured_path(tmp_path: Path):
    db_path = tmp_path / "v4.db"
    config = V4Config(
        db_path=str(db_path),
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        projects=[
            ProjectConfig(
                name="voice", repo="owner/voice",
                local_clone_path=str(tmp_path / "voice"),
            ),
        ],
    )
    bootstrap_cli_context(
        config=config,
        identity=_stub_identity(),
        git_provider_factory=lambda repo: _stub_git_factory(),
    )
    # SQLite creates the file lazily on first write; the bootstrap
    # should have applied the schema, which IS a write.
    assert db_path.exists()


def test_bootstrap_builds_one_poller_per_project(tmp_path: Path):
    config = V4Config(
        db_path=str(tmp_path / "v4.db"),
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
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
        db_path=str(tmp_path / "v4.db"),
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        projects=[
            ProjectConfig(
                name="voice", repo="owner/voice",
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
        db_path=str(tmp_path / "v4.db"),
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
        projects=[
            ProjectConfig(
                name="voice", repo="owner/voice",
                local_clone_path=str(tmp_path / "voice"),
            ),
            ProjectConfig(
                name="foreman", repo="owner/foreman",
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
        db_path=str(tmp_path / "v4.db"),
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
        operator=_operator_config(),
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
