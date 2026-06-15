"""bootstrap_cli_context — turns V4Config into a CliContext."""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from foreman.v4.bootstrap import bootstrap_cli_context
from foreman.v4.config import ProjectConfig, V4Config
from foreman.v4.logging_config import reset_logging

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


def test_bootstrap_returns_clicontext_with_all_fields(tmp_path: Path):
    config = V4Config(
        db_path=str(tmp_path / "foreman.db"),
        log_dir=str(tmp_path / "logs"),
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
