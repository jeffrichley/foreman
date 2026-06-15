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


def test_role_aliases_reviewer_spec_and_impl_to_reviewer_app():
    """``reviewer-spec`` and ``reviewer-impl`` both resolve to the
    reviewer App's credentials — the target distinction is carried via
    the subprocess CLI ``--target`` flag, not via identity."""
    clock = _MutableClock()
    registry = V4IdentityRegistry(
        apps=_apps(),
        orchestrator=_orchestrator(),
        installation_repo="owner/project",
        clock=clock,
    )

    captured: list[int] = []

    def fake_mint(app_id, private_key_path, repo_slug):
        captured.append(app_id)
        return InstallationToken(
            token=f"token-app-{app_id}",
            expires_at=int(clock.now) + 3600,
        )

    with patch(
        "foreman.v4.identity.mint_installation_token", side_effect=fake_mint,
    ):
        # Use a fresh registry instance per call by clearing the cache
        # would suppress the test's value — instead bump time past the
        # refresh window between calls so each call hits mint.
        spec_token = registry.get_role_token("reviewer-spec")
        # Force a refresh so the second variant also mints (otherwise the
        # cache-share is what we measure — which IS what the next test
        # measures). For THIS test, advance past the refresh threshold.
        clock.now += 3400
        impl_token = registry.get_role_token("reviewer-impl")

    # Both calls minted with reviewer's app_id (2), not planner (1) or
    # fixer (3) or anything else.
    assert captured == [2, 2]
    assert spec_token == "token-app-2"
    assert impl_token == "token-app-2"


def test_role_aliases_fixer_spec_and_impl_to_fixer_app():
    """``fixer-spec`` and ``fixer-impl`` both resolve to the fixer App's
    credentials. Symmetric to the reviewer aliasing test."""
    clock = _MutableClock()
    registry = V4IdentityRegistry(
        apps=_apps(),
        orchestrator=_orchestrator(),
        installation_repo="owner/project",
        clock=clock,
    )

    captured: list[int] = []

    def fake_mint(app_id, private_key_path, repo_slug):
        captured.append(app_id)
        return InstallationToken(
            token=f"token-app-{app_id}",
            expires_at=int(clock.now) + 3600,
        )

    with patch(
        "foreman.v4.identity.mint_installation_token", side_effect=fake_mint,
    ):
        spec_token = registry.get_role_token("fixer-spec")
        clock.now += 3400  # Force refresh so the second variant also mints.
        impl_token = registry.get_role_token("fixer-impl")

    # Both calls minted with fixer's app_id (3), not reviewer (2) or
    # anything else.
    assert captured == [3, 3]
    assert spec_token == "token-app-3"
    assert impl_token == "token-app-3"


def test_alias_cache_is_shared_across_target_suffixes():
    """``reviewer-spec`` mints once; the subsequent ``reviewer-impl``
    call within the refresh window hits the SAME cache entry (keyed on
    the normalized base role ``reviewer``) and does NOT mint again.

    Without this normalized cache key, target-aware variants would
    double GitHub API rate-limit consumption — see ``get_role_token``
    for the load-bearing comment.
    """
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
        spec_token = registry.get_role_token("reviewer-spec")
        clock.now += 60  # 1 minute later — well inside the safety window.
        impl_token = registry.get_role_token("reviewer-impl")

    # Single mint, shared cached token returned to both target variants.
    assert mint.call_count == 1
    assert spec_token == impl_token == "token-mint-1"


def test_planner_spec_still_raises_unknown_role():
    """Suffix-stripping only happens for the target-aware base roles
    (``reviewer``, ``fixer``). A suffix on any other role (e.g.
    ``planner-spec``) is NOT a legitimate alias and must raise — and
    the error message must surface the ORIGINAL unaliased role so the
    operator sees what the state machine actually passed."""
    registry = V4IdentityRegistry(
        apps=_apps(),
        orchestrator=_orchestrator(),
        installation_repo="owner/project",
    )
    with pytest.raises(ValueError, match="planner-spec"):
        registry.get_role_token("planner-spec")


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
