"""V4IdentityRegistry — per-role token cache backed by App installation tokens.

Tests patch ``mint_installation_token`` so no real GitHub round-trips
fire. The clock is injected so cache-vs-refresh behavior is
deterministic at the second.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from foreman.auth import AppMetadata, InstallationToken
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


def _fake_app_metadata(app_id: int, slug: str) -> AppMetadata:
    """Convenience builder for ``AppMetadata`` test fixtures.

    Mirrors the v3 ``tests/test_identity.py::_fake_app_metadata`` helper
    so the v4 ``get_role_bot_logins`` tests below stay shape-identical
    to their v3 counterparts (foreman#335 — pin the same three contracts
    on the v4 surface).
    """
    return AppMetadata(app_id=app_id, slug=slug, name="x")


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
        "foreman.v4.identity.mint_installation_token",
        side_effect=fake_mint,
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
        "foreman.v4.identity.mint_installation_token",
        side_effect=fake_mint,
    ) as mint:
        first = registry.get_role_token("planner")
        clock.now += 60  # 1 minute later — well inside the 15-minute safety window
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
        "foreman.v4.identity.mint_installation_token",
        side_effect=fake_mint,
    ) as mint:
        first = registry.get_role_token("planner")
        # Advance past the refresh threshold (3600s - 900s safety = 2700s).
        clock.now += 3400
        second = registry.get_role_token("planner")

    assert first == "token-mint-1"
    assert second == "token-mint-2"
    assert mint.call_count == 2


def test_get_role_token_mints_fresh_when_in_safety_window():
    """When the cached token has fewer than ``_REFRESH_SAFETY_SECONDS``
    (900s) remaining, the next call MUST mint a fresh token.

    Regression guard for the 2026-06-15 dogfood crash: PyGithubGitProvider
    rebuilds its cached ``Github`` client every 3000s. With the previous
    300s safety window, a token minted at t=0 (expires at t=3600) had 600s
    left when the rebuild fired at t=3000 — outside the 300s window — so
    the registry handed back the SAME expiring token. The provider's new
    client carried the old token into expiry territory and the daemon
    crashed with 401 at minute ~60. With safety = 900s, 600s < 900s, so
    the rebuild path mints fresh.
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
            # 3600s lifetime as of mint time — matches real GitHub behavior.
            expires_at=int(clock.now) + 3600,
        )

    with patch(
        "foreman.v4.identity.mint_installation_token",
        side_effect=fake_mint,
    ) as mint:
        first = registry.get_role_token("planner")
        # Advance so the cached token has exactly 700s remaining — inside
        # the 900s safety window, so a refresh MUST mint.
        clock.now += 3600 - 700
        second = registry.get_role_token("planner")

    assert first == "token-mint-1"
    assert second == "token-mint-2"
    assert mint.call_count == 2


def test_get_role_token_uses_cache_outside_safety_window():
    """When the cached token has MORE than ``_REFRESH_SAFETY_SECONDS``
    (900s) remaining, the next call MUST return the cached token without
    minting. Symmetric counter-test to the safety-window mint case —
    proves the threshold isn't accidentally too eager."""
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
        "foreman.v4.identity.mint_installation_token",
        side_effect=fake_mint,
    ) as mint:
        first = registry.get_role_token("planner")
        # Advance so the cached token has exactly 901s remaining — just
        # OUTSIDE the 900s safety window, so the cached token MUST be
        # reused (no mint).
        clock.now += 3600 - 901
        second = registry.get_role_token("planner")

    assert first == second == "token-mint-1"
    assert mint.call_count == 1


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
        "foreman.v4.identity.mint_installation_token",
        side_effect=fake_mint,
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
        "foreman.v4.identity.mint_installation_token",
        side_effect=fake_mint,
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
        "foreman.v4.identity.mint_installation_token",
        side_effect=fake_mint,
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
        "foreman.v4.identity.mint_installation_token",
        side_effect=fake_mint,
    ):
        registry.get_role_token("orchestrator")
        registry.get_role_token("planner")

    assert captured == [
        (5, "owner/the-project"),  # orchestrator's app_id, installation_repo
        (1, "owner/the-project"),  # planner's app_id, same repo
    ]


# ---------------------------------------------------------------------
# V4IdentityRegistry.get_role_bot_logins — foreman#335.
#
# Added in PR #333 to satisfy the merged-in
# ``filter_bot_self_comments(comments, identity_registry.get_role_bot_logins())``
# call sites in the three spec-side role dispatchers (Planner / Reviewer-
# on-spec / Fixer-on-spec). The method has no direct unit coverage on
# main; foreman#335 mirrors the v3 trio at
# ``packages/foreman/tests/test_identity.py:586-643`` onto this surface
# to pin the three contracts:
#
# 1. ``slug-collapse``: when all four roles share one slug, the returned
#    set has exactly one ``{slug}[bot]`` entry (set semantics
#    deduplicate accidental slug collisions).
# 2. ``per-role-distinct``: when each role's App resolves to a distinct
#    slug, all four bot logins appear in the set.
# 3. ``cache-amortized``: repeated calls reuse the per-role metadata
#    cache — the first invocation fires four ``GET /app`` calls (one
#    per role); subsequent invocations fire zero.
# ---------------------------------------------------------------------


def test_get_role_bot_logins_returns_one_login_per_role_when_slugs_collapse() -> None:
    """Default fixture: ``fetch_app_metadata`` returns the same
    ``AppMetadata`` regardless of which role asks. Set semantics
    deduplicate the four ``"foreman-planner[bot]"`` entries to one.

    The deduplication is desirable when projects accidentally share a
    slug across roles (the foreman daemon's "single GitHub App per
    role" assumption can drift in test fixtures); the rendered filter
    set stays correct."""
    fake_meta = _fake_app_metadata(app_id=1, slug="foreman-planner")
    with patch(
        "foreman.v4.identity.fetch_app_metadata",
        return_value=fake_meta,
    ):
        registry = V4IdentityRegistry(
            apps=_apps(),
            orchestrator=_orchestrator(),
            installation_repo="owner/project",
        )
        logins = registry.get_role_bot_logins()
    assert logins == {"foreman-planner[bot]"}


def test_get_role_bot_logins_returns_all_four_when_slugs_differ() -> None:
    """When each role's App resolves to a distinct slug, all four entries
    appear in the returned set. Pins the per-role metadata fetch +
    enumeration behavior so a deployment running four distinct
    foreman-* GitHub Apps gets the right filter scope."""

    def _meta_per_role(app_id: int, _key_path: object) -> AppMetadata:
        # Map app_id (which uniquely identifies each role in ``_apps``)
        # back to the role's slug. ``_apps`` assigns planner=1,
        # reviewer=2, fixer=3, worker=4.
        per_id_slug = {
            1: "foreman-planner",
            2: "foreman-reviewer",
            3: "foreman-fixer",
            4: "foreman-worker",
        }
        return AppMetadata(app_id=app_id, slug=per_id_slug[app_id], name="x")

    with patch(
        "foreman.v4.identity.fetch_app_metadata",
        side_effect=_meta_per_role,
    ):
        registry = V4IdentityRegistry(
            apps=_apps(),
            orchestrator=_orchestrator(),
            installation_repo="owner/project",
        )
        logins = registry.get_role_bot_logins()
    assert logins == {
        "foreman-planner[bot]",
        "foreman-reviewer[bot]",
        "foreman-fixer[bot]",
        "foreman-worker[bot]",
    }


def test_get_role_bot_logins_caches_metadata_per_role() -> None:
    """Repeated calls must NOT re-fetch ``GET /app`` for the same role —
    ``_get_app_metadata`` caches per-role metadata for the registry's
    lifetime so the four-call cost is amortized across the daemon's
    runtime.

    First invocation: 4 ``fetch_app_metadata`` calls (one per role).
    Subsequent invocations: 0 — every entry comes from
    ``_app_meta_cache``.
    """
    fake_meta = _fake_app_metadata(app_id=1, slug="foreman-planner")
    with patch(
        "foreman.v4.identity.fetch_app_metadata",
        return_value=fake_meta,
    ) as mock_fetch:
        registry = V4IdentityRegistry(
            apps=_apps(),
            orchestrator=_orchestrator(),
            installation_repo="owner/project",
        )
        registry.get_role_bot_logins()
        registry.get_role_bot_logins()
        registry.get_role_bot_logins()
    assert mock_fetch.call_count == 4
