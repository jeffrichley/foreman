"""Unit tests for the shared role-delegate identity selection helper."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from foreman.roles import role_identity
from foreman.v4.identity import SandboxIdentityRegistry, V4IdentityRegistry


def test_sandboxed_returns_sandbox_identity_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOREMAN_SANDBOXED", "1")
    registry = role_identity(MagicMock(), installation_repo="o/r")
    assert isinstance(registry, SandboxIdentityRegistry)


def test_unsandboxed_returns_v4_identity_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOREMAN_SANDBOXED", raising=False)
    registry = role_identity(MagicMock(), installation_repo="o/r")
    assert isinstance(registry, V4IdentityRegistry)
