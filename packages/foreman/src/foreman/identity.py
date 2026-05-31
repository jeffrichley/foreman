"""Per-role identity registry — PyGithub clients via GitHub App installation tokens.

Each role's GitHub App produces a distinct ``[bot]`` identity for commits
and PRs (e.g., ``foreman-planner[bot]``). Installation tokens are 1-hour
short-lived; this registry caches them per role and auto-refreshes when
within 5 minutes of expiry.

For walking skeleton: only the planner role is wired. Reviewer/fixer/worker
will be added during thickening.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from github import Auth, Github

from foreman.auth import InstallationToken, mint_installation_token
from foreman.config import ProjectConfig

_REFRESH_SAFETY_SECONDS = 300  # refresh 5 min before expiry


@dataclass
class _CachedClient:
    client: Github
    token: InstallationToken


class IdentityRegistry:
    """Holds per-role PyGithub clients (App-installation-token-authenticated)
    for one project."""

    def __init__(self, project: ProjectConfig) -> None:
        self._project = project
        self._cache: dict[str, _CachedClient] = {}

    def get_client(self, role: str) -> Github:
        """Return a PyGithub client authenticated as the role's bot."""
        return self._get_cached(role).client

    def get_token(self, role: str) -> str:
        """Return the current installation-token string for a role.

        Used by role dispatchers that need to inject the token into a
        subprocess environment (e.g., to make the agent's ``gh`` CLI act
        as the bot, not the parent process's identity).
        """
        return self._get_cached(role).token.token

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

    def _mint_token(self, role: str) -> InstallationToken:
        if role == "planner":
            app_id = self._project.apps.resolve_planner_app_id()
            key_path = self._project.apps.resolve_planner_private_key_path()
            return mint_installation_token(app_id, key_path, self._project.repo)
        raise ValueError(
            f"Unknown role: {role!r}. Walking skeleton only supports 'planner'."
        )
