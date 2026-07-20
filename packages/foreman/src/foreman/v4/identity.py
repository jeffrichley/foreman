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

Single source of truth for token freshness
------------------------------------------
After foreman#312, this registry is the sole arbiter of when a new
installation token is minted. ``_REFRESH_SAFETY_SECONDS`` is the only
tunable controlling how aggressively tokens are refreshed. Callers
(including ``PyGithubGitProvider``) do not maintain their own time-based
rebuild intervals — they call ``get_role_token`` on every access and
receive a fresh token whenever the registry decides one is needed. There
is no cross-file arithmetic invariant to maintain.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from foreman.auth import AppMetadata, InstallationToken, fetch_app_metadata, mint_installation_token
from foreman.v4.config import AppCredentials, AppsConfig, OrchestratorConfig

# Pre-expiry safety window: refresh the cached token when fewer than this
# many seconds remain until expiry.
#
# After foreman#312, this is the ONLY tunable controlling how aggressively
# tokens are refreshed. PyGithubGitProvider no longer has its own time-based
# rebuild interval — it delegates token-freshness to this registry on every
# _gh access. Raising or lowering this value is the single knob an operator
# needs; no matching change in PyGithubGitProvider is required.
_REFRESH_SAFETY_SECONDS = 900

_KNOWN_ROLES = ("planner", "reviewer", "fixer", "worker", "orchestrator")

# Role names the SubprocessRoleDispatcher passes to get_role_token().
# Target-aware roles (reviewer/fixer) come in with -spec or -impl
# suffixes; the App identity is the same regardless of target, so
# strip the suffix before resolving credentials.
_TARGET_SUFFIXES = ("-spec", "-impl")
_TARGET_AWARE_BASE_ROLES = ("reviewer", "fixer")


def _normalize_role(role: str) -> str:
    """Strip a target suffix if the role is one of the target-aware roles.

    ``reviewer-spec`` / ``reviewer-impl`` -> ``reviewer``
    ``fixer-spec`` / ``fixer-impl``       -> ``fixer``
    Base roles (planner / worker / orchestrator / reviewer / fixer) pass through.
    Unknown roles or invalid suffixes (e.g. ``planner-spec``) pass through
    unchanged so ``_resolve`` raises a clear error with the original name.
    """
    for suffix in _TARGET_SUFFIXES:
        if role.endswith(suffix):
            base = role.removesuffix(suffix)
            if base in _TARGET_AWARE_BASE_ROLES:
                return base
            # Suffix on a non-target-aware role (e.g. "planner-spec") —
            # leave as-is; _resolve will reject with the full original
            # name in the error message.
            return role
    return role


class V4IdentityRegistry:
    """V4-native registry of per-role GitHub App installation tokens.

    Satisfies :class:`~foreman.v4.bootstrap.IdentityProvider` (one
    method: ``get_role_token(role: str) -> str``). Caches one
    installation token per (role, repo_slug) pair; refreshes when
    within ``_REFRESH_SAFETY_SECONDS`` of expiry. This registry is
    the sole arbiter of token freshness (foreman#312); callers receive
    a fresh token on every call that falls within the safety window.

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
        self._cache: dict[tuple[str, str], InstallationToken] = {}
        self._app_meta_cache: dict[str, AppMetadata] = {}
        self._clock = clock

    def get_role_token(self, role: str) -> str:
        """Return the current installation token for ``role``.

        Supported roles: ``planner``, ``reviewer``, ``fixer``,
        ``worker``, ``orchestrator``, plus the target-aware variants
        ``reviewer-spec``, ``reviewer-impl``, ``fixer-spec``, and
        ``fixer-impl`` — which alias to their base App identity (the
        target distinction is carried via subprocess CLI flags, not
        identity). Anything else raises ``ValueError``.

        Cached tokens are reused until they're within
        ``_REFRESH_SAFETY_SECONDS`` of expiry; past that threshold the
        next call mints a fresh one. Target-aware variants share a
        single cache entry with the base role.
        """
        creds, repo_slug = self._resolve(role)
        # Cache key uses the BASE role name so reviewer-spec and
        # reviewer-impl share one cached InstallationToken (same App
        # identity). Without this, target-aware variants would mint
        # independently and double GitHub API rate-limit consumption.
        key = (_normalize_role(role), repo_slug)
        cached = self._cache.get(key)
        now = int(self._clock())
        if cached is not None and cached.expires_at - now > _REFRESH_SAFETY_SECONDS:
            return cached.token
        # Mint (or refresh) — token is missing or near expiry.
        token = mint_installation_token(
            creds.app_id,
            creds.private_key_path,
            repo_slug,
        )
        self._cache[key] = token
        return token.token

    def get_role_bot_logins(self) -> set[str]:
        """Return the GitHub login strings for the four foreman role bots.

        Each login is ``f"{slug}[bot]"`` derived from the role's
        ``AppMetadata`` (fetched via ``GET /app``). Used by the spec-side
        role dispatchers (Planner / Reviewer-on-spec / Fixer-on-spec) to
        filter out role-bot self-comments from the originating issue's
        comment stream so the bots' own previous postings don't feed
        back into subsequent LLM runs (foreman#328, agent_core#180).

        Mirrors :meth:`foreman.identity.IdentityRegistry.get_role_bot_logins`
        so v4 substrate keeps PR #331's prompt-injection feature behavior
        after the v3 substrate cutover. Per-role metadata is cached for
        the registry's lifetime; first call costs up to four
        ``GET /app`` HTTP round-trips.
        """
        return {
            f"{self._get_app_metadata(role).slug}[bot]"
            for role in ("planner", "reviewer", "fixer", "worker")
        }

    def get_app_slug(self, role: str) -> str:
        """Return the role App's bot slug, fetched via ``GET /app`` and cached."""
        return self._get_app_metadata(role).slug

    def _get_app_metadata(self, role: str) -> AppMetadata:
        cached = self._app_meta_cache.get(role)
        if cached is not None:
            return cached
        creds, _repo_slug = self._resolve(role)
        meta = fetch_app_metadata(creds.app_id, creds.private_key_path)
        self._app_meta_cache[role] = meta
        return meta

    def _resolve(self, role: str) -> tuple[AppCredentials, str]:
        """Map ``role`` to its (credentials, repo_slug) tuple.

        Target-aware variants (``reviewer-spec``, ``reviewer-impl``,
        ``fixer-spec``, ``fixer-impl``) resolve to the same App
        credentials as their base role — the target is carried via the
        subprocess CLI's ``--target`` flag, not via identity.

        Per the module-level single-installation assumption, every role
        resolves to ``self._installation_repo``. The orchestrator's
        :class:`OrchestratorConfig` carries the same ``{app_id,
        private_key_path}`` shape as :class:`AppCredentials`; we
        construct a transient :class:`AppCredentials` from it so the
        caller path stays uniform.

        The error message keeps the ORIGINAL ``role`` (not the
        normalized form) so operators debugging see exactly what the
        state machine passed in.
        """
        normalized = _normalize_role(role)
        if normalized == "planner":
            return self._apps.planner, self._installation_repo
        if normalized == "reviewer":
            return self._apps.reviewer, self._installation_repo
        if normalized == "fixer":
            return self._apps.fixer, self._installation_repo
        if normalized == "worker":
            return self._apps.worker, self._installation_repo
        if normalized == "orchestrator":
            return (
                AppCredentials(
                    app_id=self._orchestrator.app_id,
                    private_key_path=self._orchestrator.private_key_path,
                ),
                self._installation_repo,
            )
        raise ValueError(
            f"Unknown role: {role!r}. Supported: {' | '.join(_KNOWN_ROLES)}.",
        )


class SandboxIdentityError(RuntimeError):
    """Raised when a sandboxed role has no injected GH_TOKEN to authenticate with.

    Typed (not a bare ``RuntimeError``) so the fail-closed path is greppable,
    mirroring ``SandboxUnavailableError`` in :mod:`foreman.v4.sandbox`.
    """


class EnvTokenIdentity:
    """``IdentityProvider`` backed by a single injected ``GH_TOKEN``.

    Used only inside the bubblewrap sandbox, where the daemon injects the
    dispatched role's short-lived installation token as ``GH_TOKEN`` and the
    PEM keys are deliberately absent (see ``DAEMON_NEVER_BIND``). Returns that
    one token for ANY ``role`` argument: the box holds exactly one role's
    identity and nothing else, so ``role`` is inert here — the sandbox mount
    plan, not this class, is what guarantees single-role. Never reads a PEM;
    fail-closed if ``GH_TOKEN`` is missing.

    Satisfies :class:`~foreman.v4.bootstrap.IdentityProvider`.
    """

    _ENV_VAR = "GH_TOKEN"

    def get_role_token(self, role: str) -> str:
        """Return the injected ``GH_TOKEN``, ignoring ``role``.

        Args:
            role: The requested role. Inert — see the class docstring.

        Returns:
            The ``GH_TOKEN`` from the process environment.

        Raises:
            SandboxIdentityError: if ``GH_TOKEN`` is unset or empty.
        """
        token = os.environ.get(self._ENV_VAR)
        if not token:
            raise SandboxIdentityError(
                "FOREMAN_SANDBOXED is set but GH_TOKEN is empty/unset; the "
                "sandboxed role has no injected token to authenticate with. "
                "The dispatcher must set --setenv GH_TOKEN <role token>. "
                "Refusing to run."
            )
        return token


class SandboxIdentityRegistry(EnvTokenIdentity):
    """Env-backed registry for a sandboxed role subprocess.

    Extends :class:`EnvTokenIdentity` (token from ``GH_TOKEN``) with the two
    other pieces the role path needs, sourced from data the daemon injected
    into the box — never a PEM: the dispatched role's bot slug
    (``FOREMAN_BOT_SLUG``, for commit attribution) and the set of role-bot
    logins (``FOREMAN_BOT_LOGINS``, whitespace-separated, for self-comment
    filtering). Duck-types the role-facing surface of
    :class:`V4IdentityRegistry` used by ``build_role_resources`` and the role
    delegates. Fail-closed (:class:`SandboxIdentityError`) if a needed var is
    absent.
    """

    def get_app_slug(self, role: str) -> str:
        """Return the injected bot slug for ``role``.

        Args:
            role: The requested role. Inert — the box holds exactly one
                role's identity; see :class:`EnvTokenIdentity`.

        Returns:
            The ``FOREMAN_BOT_SLUG`` value from the process environment.

        Raises:
            SandboxIdentityError: if ``FOREMAN_BOT_SLUG`` is unset or empty.
        """
        slug = os.environ.get("FOREMAN_BOT_SLUG")
        if not slug:
            raise SandboxIdentityError(
                "FOREMAN_SANDBOXED is set but FOREMAN_BOT_SLUG is unset; the "
                "sandboxed role has no bot slug for commit attribution. The "
                "dispatcher must inject it. Refusing to run."
            )
        return slug

    def get_role_bot_logins(self) -> set[str]:
        """Return the injected set of role-bot login strings.

        Returns:
            The whitespace-separated tokens from ``FOREMAN_BOT_LOGINS``.

        Raises:
            SandboxIdentityError: if ``FOREMAN_BOT_LOGINS`` is unset or empty.
        """
        raw = os.environ.get("FOREMAN_BOT_LOGINS")
        if not raw:
            raise SandboxIdentityError(
                "FOREMAN_SANDBOXED is set but FOREMAN_BOT_LOGINS is unset; the "
                "sandboxed role cannot filter bot self-comments. The dispatcher "
                "must inject it. Refusing to run."
            )
        return {tok for tok in raw.split() if tok}
