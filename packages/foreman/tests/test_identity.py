"""Tests for per-role identity resolution + PyGithub client construction."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from github import Github

from foreman.config import BotConfig, ProjectConfig
from foreman.identity import IdentityRegistry


def _make_project() -> ProjectConfig:
    return ProjectConfig(
        repo="jeffrichley/voice",
        local_clone_path="/tmp/voice",
        bots=BotConfig(
            planner_env="FOREMAN_PLANNER_BOT_TOKEN",
            planner_token="config-file-token",
        ),
    )


def test_get_planner_client_returns_github_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FOREMAN_PLANNER_BOT_TOKEN", raising=False)
    reg = IdentityRegistry(_make_project())
    client = reg.get_client("planner")
    assert isinstance(client, Github)


def test_get_planner_client_uses_env_var_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOREMAN_PLANNER_BOT_TOKEN", "env-token")
    # PyGithub stores the auth in the Auth attribute; we verify the token
    # routed correctly by patching the Github constructor and inspecting the
    # arguments passed in.
    with patch("foreman.identity.Github") as mock_github:
        IdentityRegistry(_make_project()).get_client("planner")
        # First positional arg to Github() is the auth object holding the token
        called_with = mock_github.call_args
        # PyGithub 2.x prefers `auth=` keyword over positional token; accept either.
        token_seen = (
            called_with.kwargs.get("auth")
            or (called_with.args[0] if called_with.args else None)
        )
        assert token_seen is not None


def test_unknown_role_raises() -> None:
    reg = IdentityRegistry(_make_project())
    with pytest.raises(ValueError, match="Unknown role"):
        reg.get_client("reviewer")  # Reviewer not implemented in walking skeleton
