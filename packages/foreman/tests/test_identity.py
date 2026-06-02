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

from foreman.auth import AppMetadata, InstallationToken
from foreman.config import AppsConfig, OrchestratorConfig, ProjectConfig
from foreman.git_hosts.github import GitHubProvider
from foreman.identity import IdentityRegistry


def _make_project() -> ProjectConfig:
    return ProjectConfig(
        repo="jeffrichley/voice",
        local_clone_path="/tmp/voice",
        apps=AppsConfig(
            planner_app_id_env="FOREMAN_PLANNER_APP_ID",
            planner_app_id=123456,
            planner_private_key_path="/tmp/planner.pem",
            reviewer_app_id_env="FOREMAN_REVIEWER_APP_ID",
            reviewer_app_id=654321,
            reviewer_private_key_path="/tmp/reviewer.pem",
            fixer_app_id_env="FOREMAN_FIXER_APP_ID",
            fixer_app_id=777777,
            fixer_private_key_path="/tmp/fixer.pem",
            worker_app_id_env="FOREMAN_WORKER_APP_ID",
            worker_app_id=444444,
            worker_private_key_path="/tmp/worker.pem",
        ),
    )


def _make_orchestrator() -> OrchestratorConfig:
    return OrchestratorConfig(
        app_id_env="FOREMAN_ORCHESTRATOR_APP_ID",
        app_id=222222,
        private_key_path="/tmp/orchestrator.pem",
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
    with patch("foreman.identity.mint_installation_token", return_value=fake_token) as mock_mint:
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
    with patch("foreman.identity.mint_installation_token", return_value=fake_token) as mock_mint:
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
    with patch("foreman.identity.mint_installation_token", return_value=fake_token) as mock_mint:
        reg = IdentityRegistry(_make_project())
        reg.get_client("planner")
    assert mock_mint.call_args.args[0] == 888888


def test_unknown_role_raises() -> None:
    reg = IdentityRegistry(_make_project())
    with pytest.raises(ValueError, match="Unknown role"):
        reg.get_client("janitor")  # Not a known role in the walking skeleton


def test_get_client_caches_github_instance() -> None:
    """get_client called twice with a fresh token must return the same instance."""
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token):
        reg = IdentityRegistry(_make_project())
        first = reg.get_client("planner")
        second = reg.get_client("planner")
    assert first is second


# ----------------------------------------------------------------------
# get_host_provider
# ----------------------------------------------------------------------


def _fake_app_metadata(*, app_id: int = 123456, slug: str = "foreman-planner") -> AppMetadata:
    return AppMetadata(app_id=app_id, slug=slug, name="Foreman Planner")


def test_get_host_provider_returns_github_provider() -> None:
    fake_token = _fresh_token(token="ghs_installtoken_abc")
    with (
        patch("foreman.identity.mint_installation_token", return_value=fake_token),
        patch("foreman.identity.fetch_app_metadata", return_value=_fake_app_metadata()),
    ):
        reg = IdentityRegistry(_make_project())
        host = reg.get_host_provider("planner")
    assert isinstance(host, GitHubProvider)


def test_get_host_provider_passes_bot_identity_built_from_app_meta_and_token() -> None:
    fake_token = _fresh_token(token="ghs_installtoken_abc")
    fake_meta = _fake_app_metadata(app_id=987654, slug="foreman-planner")
    with (
        patch("foreman.identity.mint_installation_token", return_value=fake_token),
        patch("foreman.identity.fetch_app_metadata", return_value=fake_meta),
    ):
        reg = IdentityRegistry(_make_project())
        host = reg.get_host_provider("planner")
    # GitHubProvider stores the identity privately; tap it for verification.
    identity = host._identity  # type: ignore[attr-defined]
    assert identity.slug == "foreman-planner"
    assert identity.user_id == 987654
    assert identity.token == "ghs_installtoken_abc"


def test_app_metadata_fetched_once_per_role_across_calls() -> None:
    """``GET /app`` is stable across token refreshes — cache it for the
    registry's lifetime."""
    fake_token = _fresh_token()
    fake_meta = _fake_app_metadata()
    with (
        patch("foreman.identity.mint_installation_token", return_value=fake_token),
        patch("foreman.identity.fetch_app_metadata", return_value=fake_meta) as mock_fetch,
    ):
        reg = IdentityRegistry(_make_project())
        reg.get_host_provider("planner")
        reg.get_host_provider("planner")
        reg.get_host_provider("planner")
    assert mock_fetch.call_count == 1


def test_app_metadata_fetched_with_resolved_credentials() -> None:
    fake_token = _fresh_token()
    fake_meta = _fake_app_metadata()
    with (
        patch("foreman.identity.mint_installation_token", return_value=fake_token),
        patch("foreman.identity.fetch_app_metadata", return_value=fake_meta) as mock_fetch,
    ):
        reg = IdentityRegistry(_make_project())
        reg.get_host_provider("planner")
    args = mock_fetch.call_args.args
    assert args[0] == 123456
    assert args[1] == Path("/tmp/planner.pem")


def test_get_host_provider_unknown_role_raises() -> None:
    reg = IdentityRegistry(_make_project())
    with pytest.raises(ValueError, match="Unknown role"):
        reg.get_host_provider("janitor")


# ----------------------------------------------------------------------
# Reviewer role accessors — mirror the planner pair
# ----------------------------------------------------------------------


def test_get_reviewer_client_returns_github_instance() -> None:
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token):
        reg = IdentityRegistry(_make_project())
        client = reg.get_reviewer_client()
    assert isinstance(client, Github)


def test_get_reviewer_token_returns_installation_token_string() -> None:
    fake_token = _fresh_token(token="ghs_reviewer_installtoken_abc")
    with patch("foreman.identity.mint_installation_token", return_value=fake_token):
        reg = IdentityRegistry(_make_project())
        token = reg.get_reviewer_token()
    assert token == "ghs_reviewer_installtoken_abc"


def test_get_reviewer_mint_invoked_with_reviewer_app_credentials() -> None:
    """The reviewer accessor must resolve the reviewer-specific App fields,
    not the planner's — proves the per-role credential dispatch is wired."""
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token) as mock_mint:
        reg = IdentityRegistry(_make_project())
        reg.get_reviewer_client()
    call_args = mock_mint.call_args
    assert call_args.args[0] == 654321
    assert call_args.args[1] == Path("/tmp/reviewer.pem")
    assert call_args.args[2] == "jeffrichley/voice"


def test_reviewer_env_var_overrides_config_file_app_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-var precedence for the reviewer App id mirrors planner's."""
    monkeypatch.setenv("FOREMAN_REVIEWER_APP_ID", "999999")
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token) as mock_mint:
        reg = IdentityRegistry(_make_project())
        reg.get_reviewer_client()
    assert mock_mint.call_args.args[0] == 999999


def test_reviewer_client_and_planner_client_share_no_cache() -> None:
    """Per-role tokens must NOT cross-contaminate — each role caches its own."""
    planner_token = _fresh_token(token="ghs_planner")
    reviewer_token = _fresh_token(token="ghs_reviewer")
    # mint_installation_token gets called once per role; order depends on
    # which role is requested first.
    with patch(
        "foreman.identity.mint_installation_token",
        side_effect=[planner_token, reviewer_token],
    ):
        reg = IdentityRegistry(_make_project())
        p_token = reg.get_planner_token()
        r_token = reg.get_reviewer_token()
    assert p_token == "ghs_planner"
    assert r_token == "ghs_reviewer"


# ----------------------------------------------------------------------
# Fixer role accessors — mirror the planner / reviewer pair
# ----------------------------------------------------------------------


def test_get_fixer_client_returns_github_instance() -> None:
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token):
        reg = IdentityRegistry(_make_project())
        client = reg.get_fixer_client()
    assert isinstance(client, Github)


def test_get_fixer_token_returns_installation_token_string() -> None:
    fake_token = _fresh_token(token="ghs_fixer_installtoken_abc")
    with patch("foreman.identity.mint_installation_token", return_value=fake_token):
        reg = IdentityRegistry(_make_project())
        token = reg.get_fixer_token()
    assert token == "ghs_fixer_installtoken_abc"


def test_get_fixer_mint_invoked_with_fixer_app_credentials() -> None:
    """The fixer accessor must resolve the fixer-specific App fields,
    not the planner's or reviewer's — proves per-role credential dispatch."""
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token) as mock_mint:
        reg = IdentityRegistry(_make_project())
        reg.get_fixer_client()
    call_args = mock_mint.call_args
    assert call_args.args[0] == 777777
    assert call_args.args[1] == Path("/tmp/fixer.pem")
    assert call_args.args[2] == "jeffrichley/voice"


def test_fixer_env_var_overrides_config_file_app_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOREMAN_FIXER_APP_ID", "888888")
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token) as mock_mint:
        reg = IdentityRegistry(_make_project())
        reg.get_fixer_client()
    assert mock_mint.call_args.args[0] == 888888


def test_fixer_client_and_planner_client_share_no_cache() -> None:
    """Per-role tokens must NOT cross-contaminate — each role caches its own."""
    planner_token = _fresh_token(token="ghs_planner_fxr")
    fixer_token = _fresh_token(token="ghs_fixer_fxr")
    with patch(
        "foreman.identity.mint_installation_token",
        side_effect=[planner_token, fixer_token],
    ):
        reg = IdentityRegistry(_make_project())
        p_token = reg.get_planner_token()
        f_token = reg.get_fixer_token()
    assert p_token == "ghs_planner_fxr"
    assert f_token == "ghs_fixer_fxr"


# ----------------------------------------------------------------------
# Worker role accessors — mirror the planner / reviewer / fixer pair
# ----------------------------------------------------------------------


def test_get_worker_client_returns_github_instance() -> None:
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token):
        reg = IdentityRegistry(_make_project())
        client = reg.get_worker_client()
    assert isinstance(client, Github)


def test_get_worker_token_returns_installation_token_string() -> None:
    fake_token = _fresh_token(token="ghs_worker_installtoken_abc")
    with patch("foreman.identity.mint_installation_token", return_value=fake_token):
        reg = IdentityRegistry(_make_project())
        token = reg.get_worker_token()
    assert token == "ghs_worker_installtoken_abc"


def test_get_worker_mint_invoked_with_worker_app_credentials() -> None:
    """The worker accessor must resolve the worker-specific App fields,
    not any other role's — proves per-role credential dispatch."""
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token) as mock_mint:
        reg = IdentityRegistry(_make_project())
        reg.get_worker_client()
    call_args = mock_mint.call_args
    assert call_args.args[0] == 444444
    assert call_args.args[1] == Path("/tmp/worker.pem")
    assert call_args.args[2] == "jeffrichley/voice"


def test_worker_env_var_overrides_config_file_app_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOREMAN_WORKER_APP_ID", "555555")
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token) as mock_mint:
        reg = IdentityRegistry(_make_project())
        reg.get_worker_client()
    assert mock_mint.call_args.args[0] == 555555


def test_worker_client_and_planner_client_share_no_cache() -> None:
    """Per-role tokens must NOT cross-contaminate — each role caches its own."""
    planner_token = _fresh_token(token="ghs_planner_wrk")
    worker_token = _fresh_token(token="ghs_worker_wrk")
    with patch(
        "foreman.identity.mint_installation_token",
        side_effect=[planner_token, worker_token],
    ):
        reg = IdentityRegistry(_make_project())
        p_token = reg.get_planner_token()
        w_token = reg.get_worker_token()
    assert p_token == "ghs_planner_wrk"
    assert w_token == "ghs_worker_wrk"


# ----------------------------------------------------------------------
# Orchestrator role accessors — mirror the per-role accessor pair, but
# the credentials come from the top-level OrchestratorConfig rather
# than the project's AppsConfig.
# ----------------------------------------------------------------------


def test_get_orchestrator_client_returns_github_instance() -> None:
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token):
        reg = IdentityRegistry(_make_project(), orchestrator=_make_orchestrator())
        client = reg.get_orchestrator_client()
    assert isinstance(client, Github)


def test_get_orchestrator_token_returns_installation_token_string() -> None:
    fake_token = _fresh_token(token="ghs_orchestrator_installtoken_abc")
    with patch("foreman.identity.mint_installation_token", return_value=fake_token):
        reg = IdentityRegistry(_make_project(), orchestrator=_make_orchestrator())
        token = reg.get_orchestrator_token()
    assert token == "ghs_orchestrator_installtoken_abc"


def test_get_orchestrator_mint_invoked_with_orchestrator_app_credentials() -> None:
    """The orchestrator accessor must resolve the orchestrator-specific App
    fields (top-level config.orchestrator), not any project-scoped role's."""
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token) as mock_mint:
        reg = IdentityRegistry(_make_project(), orchestrator=_make_orchestrator())
        reg.get_orchestrator_client()
    call_args = mock_mint.call_args
    assert call_args.args[0] == 222222
    assert call_args.args[1] == Path("/tmp/orchestrator.pem")
    # The repo slug used for installation lookup comes from the project — the
    # resulting token is valid across the whole installation regardless.
    assert call_args.args[2] == "jeffrichley/voice"


def test_orchestrator_env_var_overrides_config_file_app_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-var precedence for the orchestrator App id mirrors the role bots'."""
    monkeypatch.setenv("FOREMAN_ORCHESTRATOR_APP_ID", "333333")
    fake_token = _fresh_token()
    with patch("foreman.identity.mint_installation_token", return_value=fake_token) as mock_mint:
        reg = IdentityRegistry(_make_project(), orchestrator=_make_orchestrator())
        reg.get_orchestrator_client()
    assert mock_mint.call_args.args[0] == 333333


def test_orchestrator_client_and_planner_client_share_no_cache() -> None:
    """Per-role tokens must NOT cross-contaminate — the orchestrator caches
    its own installation token, separate from any of the four role bots."""
    planner_token = _fresh_token(token="ghs_planner_orch")
    orchestrator_token = _fresh_token(token="ghs_orchestrator_orch")
    with patch(
        "foreman.identity.mint_installation_token",
        side_effect=[planner_token, orchestrator_token],
    ):
        reg = IdentityRegistry(_make_project(), orchestrator=_make_orchestrator())
        p_token = reg.get_planner_token()
        o_token = reg.get_orchestrator_token()
    assert p_token == "ghs_planner_orch"
    assert o_token == "ghs_orchestrator_orch"


def test_orchestrator_role_raises_when_no_orchestrator_config_passed() -> None:
    """Requesting the orchestrator role from a registry built without the
    orchestrator config is a clear configuration error."""
    reg = IdentityRegistry(_make_project())
    with pytest.raises(ValueError, match="orchestrator config"):
        reg.get_orchestrator_client()


def test_orchestrator_mint_called_again_when_token_near_expiry() -> None:
    """Regression test for the 1-hour-daemon-death bug (issue #44).

    The orchestrator's installation token must be refreshed when it
    enters the 5-minute pre-expiry window, exactly like the per-role
    bots — proving the daemon survives past the 1-hour TTL by routing
    every API call through the registry's refresh seam.
    """
    near_expiry = InstallationToken(
        token="ghs_orch_old", expires_at=int(time.time()) + 60
    )
    fresh = _fresh_token(token="ghs_orch_new")
    with patch(
        "foreman.identity.mint_installation_token",
        side_effect=[near_expiry, fresh],
    ) as mock_mint:
        reg = IdentityRegistry(_make_project(), orchestrator=_make_orchestrator())
        first = reg.get_orchestrator_token()
        second = reg.get_orchestrator_token()
    assert first == "ghs_orch_old"
    assert second == "ghs_orch_new"
    assert mock_mint.call_count == 2
