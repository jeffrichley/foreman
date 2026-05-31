"""Tests for per-role identity resolution + PyGithub client construction.

Identity now goes through GitHub App installation tokens, not PATs. We
mock the token-minting call and verify caching + refresh semantics.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from github import Github

from foreman.auth import InstallationToken
from foreman.config import AppsConfig, ProjectConfig
from foreman.identity import IdentityRegistry


def _make_project() -> ProjectConfig:
    return ProjectConfig(
        repo="jeffrichley/voice",
        local_clone_path="/tmp/voice",
        apps=AppsConfig(
            planner_app_id_env="FOREMAN_PLANNER_APP_ID",
            planner_app_id=123456,
            planner_private_key_path="/tmp/planner.pem",
        ),
    )


def _fresh_token(token: str = "ghs_fake", lifetime_seconds: int = 3600) -> InstallationToken:
    return InstallationToken(token=token, expires_at=int(time.time()) + lifetime_seconds)


def test_get_planner_client_returns_github_instance() -> None:
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token):
        reg = IdentityRegistry(_make_project())
        client = reg.get_client("planner")
    assert isinstance(client, Github)


def test_get_planner_token_returns_installation_token_string() -> None:
    fake_token = _fresh_token(token="ghs_installtoken_abc")
    with patch("foreman.identity.mint_installation_token", return_value=fake_token):
        reg = IdentityRegistry(_make_project())
        token = reg.get_token("planner")
    assert token == "ghs_installtoken_abc"


def test_minted_token_is_passed_to_github_auth() -> None:
    fake_token = _fresh_token(token="ghs_routed_token")
    with (
        patch("foreman.identity.mint_installation_token", return_value=fake_token),
        patch("foreman.identity.Github") as mock_github,
        patch("foreman.identity.Auth") as mock_auth,
    ):
        IdentityRegistry(_make_project()).get_client("planner")
        mock_auth.Token.assert_called_once_with("ghs_routed_token")
        mock_github.assert_called_once_with(auth=mock_auth.Token.return_value)


def test_mint_called_once_when_token_fresh() -> None:
    """Repeated lookups within token TTL should NOT re-mint."""
    fake_token = _fresh_token()
    with patch(
        "foreman.identity.mint_installation_token", return_value=fake_token
    ) as mock_mint:
        reg = IdentityRegistry(_make_project())
        reg.get_client("planner")
        reg.get_client("planner")
        reg.get_token("planner")
    assert mock_mint.call_count == 1


def test_mint_called_again_when_token_near_expiry() -> None:
    """If the cached token is inside the 5-minute refresh window, the next
    lookup must mint a new one."""
    near_expiry = InstallationToken(token="ghs_old", expires_at=int(time.time()) + 60)
    fresh = _fresh_token(token="ghs_new")
    with patch(
        "foreman.identity.mint_installation_token",
        side_effect=[near_expiry, fresh],
    ) as mock_mint:
        reg = IdentityRegistry(_make_project())
        first = reg.get_token("planner")
        second = reg.get_token("planner")
    assert first == "ghs_old"
    assert second == "ghs_new"
    assert mock_mint.call_count == 2


def test_mint_invoked_with_resolved_app_credentials() -> None:
    """IdentityRegistry must hand the App id, key path, and repo slug to the
    minting function — proves the config plumbing is wired."""
    fake_token = _fresh_token()
    with patch(
        "foreman.identity.mint_installation_token", return_value=fake_token
    ) as mock_mint:
        reg = IdentityRegistry(_make_project())
        reg.get_client("planner")
    call_args = mock_mint.call_args
    # mint_installation_token(app_id, private_key_path, repo_slug) — positional
    assert call_args.args[0] == 123456
    assert call_args.args[1] == Path("/tmp/planner.pem")
    assert call_args.args[2] == "jeffrichley/voice"


def test_env_var_overrides_config_file_app_id_at_mint_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-var precedence still works through the IdentityRegistry layer."""
    monkeypatch.setenv("FOREMAN_PLANNER_APP_ID", "888888")
    fake_token = _fresh_token()
    with patch(
        "foreman.identity.mint_installation_token", return_value=fake_token
    ) as mock_mint:
        reg = IdentityRegistry(_make_project())
        reg.get_client("planner")
    assert mock_mint.call_args.args[0] == 888888


def test_unknown_role_raises() -> None:
    reg = IdentityRegistry(_make_project())
    with pytest.raises(ValueError, match="Unknown role"):
        reg.get_client("reviewer")  # Reviewer not implemented in walking skeleton


def test_get_client_caches_github_instance() -> None:
    """get_client called twice with a fresh token must return the same instance."""
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token):
        reg = IdentityRegistry(_make_project())
        first = reg.get_client("planner")
        second = reg.get_client("planner")
    assert first is second


