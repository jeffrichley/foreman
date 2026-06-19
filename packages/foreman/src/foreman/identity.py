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

Five App identities flow through this registry:

* ``planner`` / ``reviewer`` / ``fixer`` / ``worker`` — the four per-role
  bots. Each is project-scoped: credentials come from
  ``project.apps.resolve_<role>_app_id()`` / ``resolve_<role>_private_key_path()``
  and the installation token is minted against the project's repo.
* ``orchestrator`` — the daemon's host-operation bot. It is *global* to
  one App installation rather than per-project: credentials come from
  the top-level ``config.orchestrator`` block and the resulting
  installation token is valid for every repo in the installation. The
  registry uses the project's repo slug only as the installation-id
  lookup; the resulting token still spans every repo the App is
  installed on. Passing ``orchestrator=config.orchestrator`` to
  :class:`IdentityRegistry` opts the registry into serving the
  ``"orchestrator"`` role; omit it (the default) and only the four
  per-role bots are addressable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from github import Auth, Github

from foreman.auth import (
    AppMetadata,
    InstallationToken,
    fetch_app_metadata,
    mint_installation_token,
)
from foreman.config import OrchestratorConfig, ProjectConfig
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

    def __init__(
        self,
        project: ProjectConfig,
        *,
        orchestrator: OrchestratorConfig | None = None,
    ) -> None:
        self._project = project
        self._orchestrator = orchestrator
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

    # ------------------------------------------------------------------
    # Per-role convenience accessors
    # ------------------------------------------------------------------
    def get_planner_client(self) -> Github:
        """Return the PyGithub client authenticated as the planner bot."""
        return self.get_client("planner")

    def get_planner_token(self) -> str:
        """Return the planner-bot's current installation token string."""
        return self.get_token("planner")

    def get_reviewer_client(self) -> Github:
        """Return the PyGithub client authenticated as the reviewer bot.

        The Reviewer reads spec PRs, posts review comments, and advances
        labels using this client. The same installation token also flows
        into the agent subprocess via the role dispatcher's ``env=`` kwarg
        so any ``gh`` calls the LLM makes act as the reviewer bot too.
        """
        return self.get_client("reviewer")

    def get_reviewer_token(self) -> str:
        """Return the reviewer-bot's current installation token string."""
        return self.get_token("reviewer")

    def get_fixer_client(self) -> Github:
        """Return the PyGithub client authenticated as the fixer bot.

        The Fixer applies Reviewer findings to the spec doc, commits +
        pushes to the spec branch, and posts a PR comment summarizing what
        was fixed. The installation token also flows into the agent
        subprocess via the role dispatcher's ``env=`` kwarg so any ``gh``
        / ``git push`` calls the LLM makes act as the fixer bot too.
        """
        return self.get_client("fixer")

    def get_fixer_token(self) -> str:
        """Return the fixer-bot's current installation token string."""
        return self.get_token("fixer")

    def get_worker_client(self) -> Github:
        """Return the PyGithub client authenticated as the worker bot.

        The Worker implements the spec in code, commits + pushes to an
        impl branch stacked on the spec branch, and opens the impl PR.
        The installation token also flows into the agent subprocess via
        the role dispatcher's ``env=`` kwarg so any ``gh`` / ``git push``
        calls the LLM makes act as the worker bot too.
        """
        return self.get_client("worker")

    def get_worker_token(self) -> str:
        """Return the worker-bot's current installation token string."""
        return self.get_token("worker")

    def get_orchestrator_client(self) -> Github:
        """Return the PyGithub client authenticated as the orchestrator bot.

        The daemon's host adapter uses this client for every API call —
        label management, PR merging, polling search, issue close.
        Asking on every call lets the registry's 5-minute-pre-expiry
        refresh transparently propagate, so the daemon survives past
        the 1-hour installation-token TTL.
        """
        return self.get_client("orchestrator")

    def get_orchestrator_token(self) -> str:
        """Return the orchestrator bot's current installation token."""
        return self.get_token("orchestrator")

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
        app_id, key_path = self._resolve_role_credentials(role)
        meta = fetch_app_metadata(app_id, key_path)
        self._app_meta_cache[role] = meta
        return meta

    def _mint_token(self, role: str) -> InstallationToken:
        app_id, key_path = self._resolve_role_credentials(role)
        return mint_installation_token(app_id, key_path, self._project.repo)

    def _resolve_role_credentials(self, role: str) -> tuple[int, Path]:
        """Return (app_id, private_key_path) for a known role.

        Adding a role to the walking skeleton means adding a branch here.
        Keeping the dispatch in one place ensures token-mint + metadata
        fetch always read the same App credentials per role.
        """
        if role == "planner":
            return (
                self._project.apps.resolve_planner_app_id(),
                self._project.apps.resolve_planner_private_key_path(),
            )
        if role == "reviewer":
            return (
                self._project.apps.resolve_reviewer_app_id(),
                self._project.apps.resolve_reviewer_private_key_path(),
            )
        if role == "fixer":
            return (
                self._project.apps.resolve_fixer_app_id(),
                self._project.apps.resolve_fixer_private_key_path(),
            )
        if role == "worker":
            return (
                self._project.apps.resolve_worker_app_id(),
                self._project.apps.resolve_worker_private_key_path(),
            )
        if role == "orchestrator":
            if self._orchestrator is None:
                raise ValueError(
                    "Orchestrator role requested but registry was not "
                    "constructed with orchestrator config. Pass "
                    "orchestrator=config.orchestrator at construction time."
                )
            return (
                self._orchestrator.resolve_app_id(),
                self._orchestrator.resolve_private_key_path(),
            )
        raise ValueError(
            f"Unknown role: {role!r}. Walking skeleton supports "
            "'planner', 'reviewer', 'fixer', 'worker', and 'orchestrator'."
        )

    def get_role_bot_logins(self) -> set[str]:
        """Return the GitHub login strings for the four foreman role bots.

        Each login is ``f"{slug}[bot]"`` derived from the role's
        ``AppMetadata`` (fetched via ``GET /app``). Used by the spec-side
        role dispatchers (Planner / Reviewer-on-spec / Fixer-on-spec) to
        filter out role-bot self-comments from the originating issue's
        comment stream so the bots' own previous postings don't feed
        back into subsequent LLM runs.

        Per-role metadata is cached for the registry's lifetime
        (``self._app_meta_cache``), so the cost on first call is at most
        three extra ``GET /app`` HTTP calls (the calling role's metadata
        is already cached); subsequent calls are free.
        """
        return {
            f"{self._get_app_metadata(role).slug}[bot]"
            for role in ("planner", "reviewer", "fixer", "worker")
        }

    def get_role_identity_env(self, role: str) -> dict[str, str]:
        """Return GIT_AUTHOR_*/GIT_COMMITTER_* env vars for a role's bot identity.

        Used by the v3 role dispatcher (V3GitHubHost.dispatch_role) so the
        LLM-driven git commit calls inside the role subprocess attribute to
        the correct bot (e.g. foreman-worker[bot]) regardless of what
        .git/config says in the worktree.

        Constructed from the role's App metadata (slug + numeric id) without
        minting a fresh installation token --- the metadata is cached in the
        registry for its lifetime.
        """
        meta = self._get_app_metadata(role)
        bot_name = f"{meta.slug}[bot]"
        bot_email = f"{meta.app_id}+{meta.slug}[bot]@users.noreply.github.com"
        return {
            "GIT_AUTHOR_NAME": bot_name,
            "GIT_AUTHOR_EMAIL": bot_email,
            "GIT_COMMITTER_NAME": bot_name,
            "GIT_COMMITTER_EMAIL": bot_email,
        }
