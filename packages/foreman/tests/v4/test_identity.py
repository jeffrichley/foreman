"""V4IdentityRegistry — per-role token cache backed by App installation tokens.

Tests patch ``mint_installation_token`` so no real GitHub round-trips
fire. The clock is injected so cache-vs-refresh behavior is
deterministic at the second.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from foreman.auth import InstallationToken
from foreman.v4.config import AppCredentials, AppsConfig, OrchestratorConfig
from foreman.v4.identity import V4IdentityRegistry


def _apps() -> AppsConfig:
    """Four per-role apps with distinct app_ids so test assertions can
    tell them apart from the patched mint side_effect."""
    return AppsConfig(
        planner=AppCredentials(app_id=1, private_key_path="/tmp/planner.pem"),
        reviewer=AppCredentials(app_id=2, private_key_path="/tmp/reviewer.pem"),
        fixer=AppCredentials(app_id=3, private_key_path="/tmp/fixer.pem"),
        worker=AppCredentials(app_id=4, private_key_path="/tmp/worker.pem"),
    )


def _orchestrator() -> OrchestratorConfig:
    return OrchestratorConfig(app_id=5, private_key_path="/tmp/orchestrator.pem")


class _MutableClock:
    """Injectable clock — tests advance ``now`` to cross expiry boundaries."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_get_role_token_for_each_role():
    """Each of the five roles returns a role-specific token."""
    clock = _MutableClock()
    registry = V4IdentityRegistry(
        apps=_apps(),
        orchestrator=_orchestrator(),
        installation_repo="owner/project",
        clock=clock,
    )

    def fake_mint(app_id, private_key_path, repo_slug):
        # Token string encodes which role's credentials minted it, so
        # the test assertions can be unambiguous.
        return InstallationToken(
            token=f"token-app-{app_id}",
            expires_at=int(clock.now) + 3600,
        )

    with patch(
        "foreman.v4.identity.mint_installation_token", side_effect=fake_mint,
    ):
        assert registry.get_role_token("planner") == "token-app-1"
        assert registry.get_role_token("reviewer") == "token-app-2"
        assert registry.get_role_token("fixer") == "token-app-3"
        assert registry.get_role_token("worker") == "token-app-4"
        assert registry.get_role_token("orchestrator") == "token-app-5"


def test_token_cached_within_refresh_window():
    """A second call inside the safety window reuses the cached token —
    mint is called exactly once for the role."""
    clock = _MutableClock()
    registry = V4IdentityRegistry(
        apps=_apps(),
        orchestrator=_orchestrator(),
        installation_repo="owner/project",
        clock=clock,
    )

    def fake_mint(app_id, private_key_path, repo_slug):
        return InstallationToken(
            token="initial-token",
            expires_at=int(clock.now) + 3600,
        )

    with patch(
        "foreman.v4.identity.mint_installation_token", side_effect=fake_mint,
    ) as mint:
        first = registry.get_role_token("planner")
        clock.now += 60  # 1 minute later — well inside the 5-minute safety window
        second = registry.get_role_token("planner")

    assert first == second == "initial-token"
    assert mint.call_count == 1


def test_token_refreshed_when_near_expiry():
    """Once the clock advances past ``expires_at - _REFRESH_SAFETY_SECONDS``,
    the next call mints a fresh token."""
    clock = _MutableClock()
    registry = V4IdentityRegistry(
        apps=_apps(),
        orchestrator=_orchestrator(),
        installation_repo="owner/project",
        clock=clock,
    )

    counter = {"n": 0}

    def fake_mint(app_id, private_key_path, repo_slug):
        counter["n"] += 1
        return InstallationToken(
            token=f"token-mint-{counter['n']}",
            expires_at=int(clock.now) + 3600,
        )

    with patch(
        "foreman.v4.identity.mint_installation_token", side_effect=fake_mint,
    ) as mint:
        first = registry.get_role_token("planner")
        # Advance past the refresh threshold (3600s - 300s safety = 3300s).
        clock.now += 3400
        second = registry.get_role_token("planner")

    assert first == "token-mint-1"
    assert second == "token-mint-2"
    assert mint.call_count == 2


def test_unknown_role_raises():
    """Anything outside the five supported roles raises ``ValueError``."""
    registry = V4IdentityRegistry(
        apps=_apps(),
        orchestrator=_orchestrator(),
        installation_repo="owner/project",
    )
    with pytest.raises(ValueError, match="Unknown role"):
        registry.get_role_token("not-a-role")


def test_orchestrator_uses_same_installation_repo():
    """The orchestrator's mint call passes ``installation_repo`` (the
    single-installation-per-role-bot assumption). Per-role bots use the
    same repo — see the module docstring for why."""
    clock = _MutableClock()
    registry = V4IdentityRegistry(
        apps=_apps(),
        orchestrator=_orchestrator(),
        installation_repo="owner/the-project",
        clock=clock,
    )

    captured: list[tuple[int, str]] = []

    def fake_mint(app_id, private_key_path, repo_slug):
        captured.append((app_id, repo_slug))
        return InstallationToken(
            token=f"token-{app_id}",
            expires_at=int(clock.now) + 3600,
        )

    with patch(
        "foreman.v4.identity.mint_installation_token", side_effect=fake_mint,
    ):
        registry.get_role_token("orchestrator")
        registry.get_role_token("planner")

    assert captured == [
        (5, "owner/the-project"),  # orchestrator's app_id, installation_repo
        (1, "owner/the-project"),  # planner's app_id, same repo
    ]
