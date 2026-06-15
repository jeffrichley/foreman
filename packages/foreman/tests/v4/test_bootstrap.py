"""bootstrap_cli_context — turns V4Config into a CliContext."""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from foreman.v4.bootstrap import bootstrap_cli_context
from foreman.v4.config import (
    AppCredentials,
    AppsConfig,
    OrchestratorConfig,
    ProjectConfig,
    V4Config,
)
from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.logging_config import reset_logging
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.observers.label_observability import LabelObservabilityObserver
from foreman.v4.observers.metrics import MetricsObserver
from foreman.v4.observers.structured_log import StructuredLogObserver

_V4_LOGGER_NAMES = (
    "foreman.v4",
    "foreman.v4.transitions",
    "foreman.v4.event_bus",
)


@pytest.fixture(autouse=True)
def _reset_logging():
    # bootstrap_cli_context calls configure_logging, which mutates the
    # `foreman.v4.*` loggers (handlers + propagate=False). Snapshot
    # propagate before each test, then restore + drop handlers after so
    # later caplog-based tests can capture warnings on these loggers.
    snapshots = {n: logging.getLogger(n).propagate for n in _V4_LOGGER_NAMES}
    yield
    reset_logging()
    for name, propagate in snapshots.items():
        logging.getLogger(name).propagate = propagate


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


def test_bootstrap_returns_clicontext_with_all_fields(tmp_path: Path):
    config = V4Config(
        db_path=str(tmp_path / "foreman.db"),
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
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
    assert len(subscribers) == 4, (
        f"expected 4 observers (structured-log, event-archive, "
        f"label-observability, metrics), got {len(subscribers)}"
    )

    observer_types = {type(s) for s in subscribers}
    assert StructuredLogObserver in observer_types
    assert EventArchiveObserver in observer_types
    assert LabelObservabilityObserver in observer_types
    assert MetricsObserver in observer_types


def test_bootstrap_skips_label_observer_when_no_projects(tmp_path: Path):
    """Zero-project configs have no GitProvider to construct a
    LabelObservabilityObserver from. The other three observers still
    land on the bus so the surface remains observable."""
    config = V4Config(
        db_path=str(tmp_path / "v4.db"),
        log_dir=str(tmp_path / "logs"),
        apps=_apps_config(),
        orchestrator=_orchestrator_config(),
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
