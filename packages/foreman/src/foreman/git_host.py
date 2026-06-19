"""Git-host abstraction layer.

All deterministic git + host-platform operations (clone fetches, commits,
pushes, PR creation, label changes) flow through :class:`GitHostProvider`.
Role modules (Planner today; Reviewer / Fixer / Worker later) talk to this
facade — not to PyGithub or ``git`` directly — so swapping GitHub for
GitLab or Bitbucket is a one-file change.

The LLM never sees this interface. The "Looper pattern" filed as Foreman
issue #8 says deterministic operations belong in core: the LLM only returns
spec content + PR metadata; core dispatches the side effects.

Walking-skeleton scope: only the planner role's needs are implemented in
:mod:`foreman.git_hosts.github`. Reviewer / Fixer / Worker operations will
extend this interface in lockstep with their role implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class BotIdentity:
    """Per-role bot identity for git commit attribution + push auth.

    Constructed from the role's GitHub App metadata (``GET /app``) and a
    fresh installation token. The provider uses the slug + numeric id to
    set ``git config user.name`` and ``user.email`` inside the worktree,
    and the token to authenticate ``git push`` and any API calls.

    Tokens are 1-hour TTL; a new BotIdentity is constructed each time the
    underlying token is minted/refreshed by :class:`~foreman.identity.IdentityRegistry`.
    """

    slug: str
    """App slug, e.g. ``"foreman-planner"``. Used for ``<slug>[bot]`` attribution."""

    user_id: int
    """Numeric App id. Used in the GitHub noreply email convention."""

    token: str
    """Installation token (1-hour TTL) for push auth + host API calls."""


@dataclass(frozen=True)
class IssueRef:
    """Issue payload returned by :meth:`GitHostProvider.get_issue`."""

    number: int
    title: str
    body: str
    labels: list[str]
    repo_slug: str


@dataclass(frozen=True)
class CommentRef:
    """Issue-comment payload returned by :meth:`GitHostProvider.get_issue_comments`.

    Frozen so role-side code can treat it as an immutable wire shape. The
    foreman role-bot self-comments are NOT filtered at the provider layer
    — that's a role-policy concern. See
    :func:`foreman.roles._prompt_helpers.filter_bot_self_comments` for the
    filter.
    """

    author_login: str
    posted_at: datetime
    body: str


@dataclass(frozen=True)
class PRRef:
    """PR payload returned by :meth:`GitHostProvider.open_pull_request`."""

    number: int
    url: str
    title: str
    body: str
    branch: str
    base_branch: str
    repo_slug: str


class GitHostProvider(ABC):
    """Abstract git-host provider.

    Concrete implementations (e.g., :class:`~foreman.git_hosts.github.GitHubProvider`)
    receive a :class:`BotIdentity` at construction so all operations attribute
    correctly. A future ``GitLabProvider`` would expose the same surface and
    drop in without role-module changes.
    """

    @abstractmethod
    def get_issue(self, repo_slug: str, issue_number: int) -> IssueRef:
        """Fetch issue title, body, and labels."""

    @abstractmethod
    def get_issue_comments(self, repo_slug: str, issue_number: int) -> list[CommentRef]:
        """Fetch the issue's comments in chronological order (oldest first).

        No filtering is applied at the provider layer; foreman role-bot
        self-comments and any other policy-driven exclusions are the
        caller's responsibility (see
        :func:`foreman.roles._prompt_helpers.filter_bot_self_comments`).
        """

    @abstractmethod
    def get_default_branch(self, repo_slug: str) -> str:
        """Return the repository's default branch (e.g., ``"main"``)."""

    @abstractmethod
    def commit_files_to_worktree(
        self,
        worktree_path: Path,
        files: dict[str, str],
        message: str,
    ) -> str:
        """Write ``files`` (relpath → content), ``git add`` + ``git commit``.

        Returns the new commit SHA. The commit attributes to the role's
        bot identity (e.g., ``foreman-planner[bot]``) via env-var
        injection on the commit subprocess — implementations must NOT
        persist ``user.name`` / ``user.email`` into ``.git/config``
        because git worktrees share that file with the parent repo and
        the leaked identity would mis-attribute subsequent human
        commits (foreman#53).
        """

    @abstractmethod
    def push_branch(self, worktree_path: Path, branch: str) -> None:
        """Push ``branch`` to the remote, authenticated by the bot's token.

        Implementations typically construct an HTTPS URL embedding the
        installation token (e.g., ``https://x-access-token:<token>@.../repo.git``).
        """

    @abstractmethod
    def open_pull_request(
        self,
        repo_slug: str,
        title: str,
        body: str,
        base: str,
        head: str,
    ) -> PRRef:
        """Open a pull request and return its reference."""

    @abstractmethod
    def update_issue_labels(
        self,
        repo_slug: str,
        issue_number: int,
        add: list[str],
        remove: list[str],
    ) -> None:
        """Add and/or remove labels on an issue."""

    @abstractmethod
    def post_issue_comment(
        self,
        repo_slug: str,
        issue_number: int,
        body: str,
    ) -> None:
        """Post a comment on an issue (foreman#229 — defensive exception
        handler surface). Used by the Planner role runner when an
        unhandled exception escapes the body wrap; the comment carries
        the exception type + traceback so an operator can diagnose
        without spelunking daemon logs.
        """
