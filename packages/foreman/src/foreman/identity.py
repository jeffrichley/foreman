"""Per-role identity registry — PyGithub clients + GitHostProviders.

Each role's GitHub App produces a distinct ``[bot]`` identity for commits
and PRs (e.g., ``foreman-planner[bot]``). Installation tokens are 1-hour
short-lived; this registry caches them per role and auto-refreshes when
within 5 minutes of expiry.

Two surfaces share the same cached installation token:

* :meth:`IdentityRegistry.get_client` — raw PyGithub client (kept for
  callers that still need it, e.g., admin-time scripts).
* :meth:`IdentityRegistry.get_host_provider` — high-level
  :class:`~foreman.git_host.GitHostProvider` the role dispatchers use.

The App metadata (slug + numeric id, fetched via ``GET /app``) is cached
*per role for the registry's lifetime* — slugs change only if the App is
renamed in GitHub's UI, which is an admin event. The installation token is
refreshed independently; a fresh :class:`~foreman.git_host.BotIdentity` is
constructed each time the token rolls over.

For walking skeleton: only the planner role is wired. Reviewer/fixer/worker
will be added during thickening.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from github import Auth, Github

from foreman.auth import (
    AppMetadata,
    InstallationToken,
    fetch_app_metadata,
    mint_installation_token,
)
from foreman.config import ProjectConfig
from foreman.git_host import BotIdentity, GitHostProvider
from foreman.git_hosts.github import GitHubProvider

_REFRESH_SAFETY_SECONDS = 300  # refresh 5 min before expiry


@dataclass
class _CachedClient:
    client: Github
    token: InstallationToken


class IdentityRegistry:
    """Holds per-role PyGithub clients + host providers (App-installation-token
    authenticated) for one project."""

    def __init__(self, project: ProjectConfig) -> None:
        self._project = project
        self._cache: dict[str, _CachedClient] = {}
        # App metadata is stable across token refreshes — cache for the
        # registry's lifetime.
        self._app_meta_cache: dict[str, AppMetadata] = {}

    def get_client(self, role: str) -> Github:
        """Return a PyGithub client authenticated as the role's bot."""
        return self._get_cached(role).client

    def get_token(self, role: str) -> str:
        """Return the current installation-token string for a role.

        Retained for callers that still need raw token access (e.g., legacy
        env-injection paths). New code should prefer
        :meth:`get_host_provider` — the host abstraction is the seam between
        Foreman core and any specific git platform.
        """
        return self._get_cached(role).token.token

    def get_host_provider(self, role: str) -> GitHostProvider:
        """Return a :class:`~foreman.git_host.GitHostProvider` for the role.

        Currently always a :class:`~foreman.git_hosts.github.GitHubProvider`.
        Future: dispatch on ``project.host`` to return a GitLab provider, etc.
        """
        cached = self._get_cached(role)
        meta = self._get_app_metadata(role)
        identity = BotIdentity(
            slug=meta.slug,
            user_id=meta.app_id,
            token=cached.token.token,
        )
        return GitHubProvider(identity=identity, client=cached.client)

    def _get_cached(self, role: str) -> _CachedClient:
        cached = self._cache.get(role)
        now = int(time.time())
        if cached and cached.token.expires_at - now > _REFRESH_SAFETY_SECONDS:
            return cached
        # Mint (or refresh) — token is missing or near expiry.
        token = self._mint_token(role)
        client = Github(auth=Auth.Token(token.token))
        new_cached = _CachedClient(client=client, token=token)
        self._cache[role] = new_cached
        return new_cached

    def _get_app_metadata(self, role: str) -> AppMetadata:
        cached = self._app_meta_cache.get(role)
        if cached is not None:
            return cached
        if role == "planner":
            app_id = self._project.apps.resolve_planner_app_id()
            key_path = self._project.apps.resolve_planner_private_key_path()
            meta = fetch_app_metadata(app_id, key_path)
            self._app_meta_cache[role] = meta
            return meta
        raise ValueError(
            f"Unknown role: {role!r}. Walking skeleton only supports 'planner'."
        )

    def _mint_token(self, role: str) -> InstallationToken:
        if role == "planner":
            app_id = self._project.apps.resolve_planner_app_id()
            key_path = self._project.apps.resolve_planner_private_key_path()
            return mint_installation_token(app_id, key_path, self._project.repo)
        raise ValueError(
            f"Unknown role: {role!r}. Walking skeleton only supports 'planner'."
        )
