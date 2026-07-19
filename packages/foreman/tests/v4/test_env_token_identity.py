"""Unit tests for EnvTokenIdentity — the sandbox's GH_TOKEN-backed identity."""

from __future__ import annotations

import pytest

from foreman.v4.identity import EnvTokenIdentity, SandboxIdentityError


def test_returns_injected_token_for_any_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghs_INJECTED")
    ident = EnvTokenIdentity()
    # The box holds exactly one token; the role argument is inert here.
    assert ident.get_role_token("planner") == "ghs_INJECTED"
    assert ident.get_role_token("orchestrator") == "ghs_INJECTED"
    assert ident.get_role_token("reviewer") == "ghs_INJECTED"


def test_raises_when_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(SandboxIdentityError):
        EnvTokenIdentity().get_role_token("planner")


def test_raises_when_token_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "")
    with pytest.raises(SandboxIdentityError):
        EnvTokenIdentity().get_role_token("planner")


def test_satisfies_identity_provider_protocol() -> None:
    from foreman.v4.bootstrap import IdentityProvider

    ident: IdentityProvider = EnvTokenIdentity()  # structural typing check
    assert callable(ident.get_role_token)
