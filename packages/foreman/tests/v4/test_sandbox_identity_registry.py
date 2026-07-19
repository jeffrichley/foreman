"""Unit tests for SandboxIdentityRegistry — env-backed identity for the box."""

from __future__ import annotations

import pytest

from foreman.v4.identity import SandboxIdentityError, SandboxIdentityRegistry


def test_get_role_token_returns_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghs_X")
    assert SandboxIdentityRegistry().get_role_token("planner") == "ghs_X"


def test_get_app_slug_returns_env_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOREMAN_BOT_SLUG", "my-planner-app")
    assert SandboxIdentityRegistry().get_app_slug("planner") == "my-planner-app"


def test_get_app_slug_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOREMAN_BOT_SLUG", raising=False)
    with pytest.raises(SandboxIdentityError):
        SandboxIdentityRegistry().get_app_slug("planner")


def test_get_role_bot_logins_parses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOREMAN_BOT_LOGINS", "a[bot] b[bot]  c[bot]")
    assert SandboxIdentityRegistry().get_role_bot_logins() == {"a[bot]", "b[bot]", "c[bot]"}


def test_get_role_bot_logins_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOREMAN_BOT_LOGINS", raising=False)
    with pytest.raises(SandboxIdentityError):
        SandboxIdentityRegistry().get_role_bot_logins()
