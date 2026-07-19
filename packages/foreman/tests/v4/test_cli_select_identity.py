"""Unit tests for the sandbox/PEM identity selection in the CLI entrypoint."""

from __future__ import annotations

from unittest.mock import MagicMock

from foreman.v4.cli import _select_identity
from foreman.v4.identity import EnvTokenIdentity, V4IdentityRegistry


def test_sandboxed_returns_env_token_identity_and_skips_clone() -> None:
    ident, run_startup_clone = _select_identity(config=MagicMock(), projects=[], sandboxed=True)
    assert isinstance(ident, EnvTokenIdentity)
    assert run_startup_clone is False  # sandbox skips the daemon clone loop


def test_sandboxed_does_not_index_projects() -> None:
    # projects=[] must not raise — sandbox mode never reads projects[0].repo.
    ident, _ = _select_identity(config=MagicMock(), projects=[], sandboxed=True)
    assert isinstance(ident, EnvTokenIdentity)


def test_unsandboxed_returns_registry_and_runs_clone() -> None:
    project = MagicMock()
    project.repo = "owner/repo"
    ident, run_startup_clone = _select_identity(
        config=MagicMock(), projects=[project], sandboxed=False
    )
    assert isinstance(ident, V4IdentityRegistry)
    assert run_startup_clone is True
