"""V4-native per-role token registry.

This module is the v4 substitute for the legacy
:class:`foreman.identity.IdentityRegistry`. It satisfies the
:class:`~foreman.v4.bootstrap.IdentityProvider` Protocol (one method:
``get_role_token(role: str) -> str``) by minting + caching GitHub App
installation tokens via :func:`foreman.auth.mint_installation_token`.

Why not reuse v3's ``IdentityRegistry``
--------------------------------------
1. **Shape mismatch.** v3's registry is per-*project*, embedding the
   ``[apps.<role>]`` block inside each project's TOML. v4 inverts
   that — App credentials are *global*, shared across all projects the
   daemon manages, because the same GitHub App is installed in every
   v4-managed repo (one App per role, regardless of which project the
   ticket belongs to).
2. **Method-name mismatch.** v3 exposes ``get_token(role)``; v4's
   ``IdentityProvider`` Protocol names the contract ``get_role_token``.
   That's not just a rename — v4's method returns a bare string token
   (``str``), v3's returns a ``BotIdentity`` carrying slug + user-id +
   token for commit-attribution wiring. The v4 ``SubprocessRoleDispatcher``
   only needs the token (it injects via ``GH_TOKEN`` env); the slug +
   user-id live in the subprocess-side identity layer.
3. **v4 isolation discipline.** v4 modules must not import the legacy
   substrate (see ``foreman.v4.tests.test_isolation.KILL_SET``).
   ``foreman.identity`` is in the *survival* set so v4 *could* import
   from it — but doing so would couple v4 to v3's per-project shape
   and BotIdentity surface, which the v4 design deliberately walked
   away from. This module owns its own thin cache; v3's registry is
   left alone for v2/v3 callers.

Single-installation-per-role-bot assumption
-------------------------------------------
GitHub App installation tokens are scoped per-(App, installation-id).
The installation-id lookup needs *some* repo the App is installed in.
v4 assumes a single-org-style deployment: every v4-managed repo lives
under the same GitHub org (or user account) and the same per-role App
is installed in all of them, so any project's repo works for the
installation-id lookup. ``main()`` picks the first project's repo at
:class:`V4IdentityRegistry` construction time; the same repo is used
for the orchestrator and for all four per-role bots.

If a future v4 deployment needs multi-installation support (e.g., an
org-wide App installed at ``orgA/*`` *and* a separately-installed App
at ``orgB/*``), the registry would need a project → repo mapping at
``get_role_token`` time. That's not in scope for v4 v1; document the
constraint here so the next refactor finds it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from foreman.auth import InstallationToken, mint_installation_token
from foreman.v4.config import AppCredentials, AppsConfig, OrchestratorConfig

# Match v3's 5-minute pre-expiry refresh window (foreman.identity uses
# the same constant). Keeps token-mint amortized; never serve a token
# that is about to expire mid-request.
_REFRESH_SAFETY_SECONDS = 300

_KNOWN_ROLES = ("planner", "reviewer", "fixer", "worker", "orchestrator")


@dataclass
class _CachedToken:
    """Internal cache cell: a minted token + the inputs it was minted for.

    The (role, repo_slug) key in the cache dict is enough to identify
    the entry; the token's ``expires_at`` drives refresh decisions.
    """

    token: InstallationToken


class V4IdentityRegistry:
    """V4-native registry of per-role GitHub App installation tokens.

    Satisfies :class:`~foreman.v4.bootstrap.IdentityProvider` (one
    method: ``get_role_token(role: str) -> str``). Caches one
    installation token per (role, repo_slug) pair; refreshes when
    within 5 minutes of expiry.

    All five roles (``planner``, ``reviewer``, ``fixer``, ``worker``,
    ``orchestrator``) resolve their installation-id lookup against
    ``installation_repo`` — the project repo passed at construction
    time. See the module-level "single-installation-per-role-bot
    assumption" docstring for why.

    Parameters
    ----------
    apps:
        Per-role App credentials (planner / reviewer / fixer / worker).
    orchestrator:
        Orchestrator App credentials (same shape as a per-role app).
    installation_repo:
        The ``owner/name`` repo used to look up each App's installation
        id. Single-installation-per-role-bot assumption: every
        v4-managed repo shares the same per-role App installation, so
        any project's repo works here. main() passes the first
        project's repo.
    clock:
        Injectable seconds-since-epoch source for tests. Defaults to
        :func:`time.time`.
    """

    def __init__(
        self,
        *,
        apps: AppsConfig,
        orchestrator: OrchestratorConfig,
        installation_repo: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._apps = apps
        self._orchestrator = orchestrator
        self._installation_repo = installation_repo
        self._cache: dict[tuple[str, str], _CachedToken] = {}
        self._clock = clock

    def get_role_token(self, role: str) -> str:
        """Return the current installation token for ``role``.

        Supported roles: ``planner``, ``reviewer``, ``fixer``,
        ``worker``, ``orchestrator``. Anything else raises ``ValueError``.

        Cached tokens are reused until they're within
        ``_REFRESH_SAFETY_SECONDS`` of expiry; past that threshold the
        next call mints a fresh one.
        """
        creds, repo_slug = self._resolve(role)
        key = (role, repo_slug)
        cached = self._cache.get(key)
        now = int(self._clock())
        if cached is not None and cached.token.expires_at - now > _REFRESH_SAFETY_SECONDS:
            return cached.token.token
        # Mint (or refresh) — token is missing or near expiry.
        token = mint_installation_token(
            creds.app_id, Path(creds.private_key_path), repo_slug,
        )
        self._cache[key] = _CachedToken(token=token)
        return token.token

    def _resolve(self, role: str) -> tuple[AppCredentials, str]:
        """Map ``role`` to its (credentials, repo_slug) tuple.

        Per the module-level single-installation assumption, every role
        resolves to ``self._installation_repo``. The orchestrator's
        :class:`OrchestratorConfig` carries the same ``{app_id,
        private_key_path}`` shape as :class:`AppCredentials`; we
        construct a transient :class:`AppCredentials` from it so the
        caller path stays uniform.
        """
        if role == "planner":
            return self._apps.planner, self._installation_repo
        if role == "reviewer":
            return self._apps.reviewer, self._installation_repo
        if role == "fixer":
            return self._apps.fixer, self._installation_repo
        if role == "worker":
            return self._apps.worker, self._installation_repo
        if role == "orchestrator":
            return (
                AppCredentials(
                    app_id=self._orchestrator.app_id,
                    private_key_path=self._orchestrator.private_key_path,
                ),
                self._installation_repo,
            )
        raise ValueError(
            f"Unknown role: {role!r}. Supported: "
            f"{' | '.join(_KNOWN_ROLES)}.",
        )
