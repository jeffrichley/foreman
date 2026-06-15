"""PyGithubGitProvider — production GitProvider backed by PyGithub.

Tests use FakeGitProvider (Task 3.2); production uses this. The seam
matches the Protocol from foreman.v4.git_provider.

Token-refresh seam
------------------
GitHub App installation tokens expire after 1 hour. v4's
:class:`~foreman.v4.identity.V4IdentityRegistry` mints + caches them
with a 5-minute pre-expiry refresh, but that refresh is only useful if
the consumer ASKS for a fresh token. ``PyGithub.Github(token)`` stores
the token at construction and never reaches back for a new one — so a
long-running daemon that constructs the client once at bootstrap dies
at minute ~60 with ``BadCredentialsException: 401``.

The fix is a factory seam: ``PyGithubGitProvider`` takes a callable
that mints a fresh ``Github`` client (which internally calls
``identity.get_role_token(...)``), and rebuilds the cached client when
it's older than ``refresh_after_seconds`` (default 50 min — well
inside the 1-hour TTL with safety margin). The :class:`Repository`
handle and PyGithub's name-mangled GraphQL requester both live INSIDE
each ``Github`` instance, so rebuilding the client invalidates both.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from github import GithubException

from foreman.v4.git_provider import MergeVerdict, PRNotFoundError, PRState

if TYPE_CHECKING:
    from github import Github
    from github.Repository import Repository

_CI_PASSING_STATES = frozenset({"clean", "unstable"})

# Default refresh window: 50 minutes (3000 seconds). Load-bearing — the
# GitHub App installation token TTL is 1 hour (3600s); rebuilding the
# client at 50 min keeps us comfortably inside that window even if the
# next API call happens a few minutes later. Do not raise this above
# ~3300s without also revisiting V4IdentityRegistry's 5-min pre-expiry
# refresh safety margin.
_DEFAULT_REFRESH_AFTER_SECONDS = 3000.0


class PyGithubGitProvider:
    def __init__(
        self,
        *,
        github_factory: Callable[[], Github],
        repo_full_name: str,
        clock: Callable[[], float] = time.time,
        refresh_after_seconds: float = _DEFAULT_REFRESH_AFTER_SECONDS,
    ) -> None:
        """Construct the provider.

        Parameters
        ----------
        github_factory:
            Zero-arg callable that returns a fresh ``Github`` client.
            Called lazily on first ``_gh`` access AND on each refresh
            past ``refresh_after_seconds``. Production wires this to
            ``lambda: Github(identity.get_role_token("orchestrator"))``
            so every rebuild pulls a fresh installation token from the
            identity registry.
        repo_full_name:
            ``owner/name`` slug, e.g. ``"jeffrichley/algokit"``.
        clock:
            Injectable seconds-since-epoch source. Defaults to
            :func:`time.time`. Tests monkey-patch this to simulate
            clock advance without sleeping.
        refresh_after_seconds:
            How long a cached ``Github`` client can live before the
            next ``_gh`` access rebuilds it. Default 50 min; see the
            module-level docstring for why this is load-bearing.
        """
        self._github_factory = github_factory
        self._repo_full_name = repo_full_name
        self._clock = clock
        self._refresh_after = refresh_after_seconds
        self._cached_github: Github | None = None
        self._cached_at: float | None = None
        self._cached_repo: Repository | None = None

    @property
    def _gh(self) -> Github:
        """Return a ``Github`` client with a non-expired token.

        Rebuilds the cached client (and invalidates the cached
        :class:`Repository` handle) when the cached client is older
        than ``refresh_after_seconds``. Lazy: the factory is NOT
        invoked at construction time — only on first access.

        PyGithub stashes its private GraphQL requester at
        ``_Github__requester`` on each ``Github`` instance, so any
        downstream method that re-reads ``self._gh._Github__requester``
        after a refresh automatically picks up the new requester.
        Callers MUST go through ``self._gh`` for every API access; do
        not cache a ``Github`` reference outside this property.
        """
        now = self._clock()
        if (
            self._cached_github is None
            or self._cached_at is None
            or (now - self._cached_at) > self._refresh_after
        ):
            self._cached_github = self._github_factory()
            self._cached_at = now
            # Repository handle is scoped to the OLD Github client's
            # requester; invalidate so the next ``_repo`` access
            # re-fetches via the freshly-built client.
            self._cached_repo = None
        return self._cached_github

    @property
    def _repo(self) -> Repository:
        """Return a :class:`Repository` handle for the configured repo.

        Cached alongside ``_gh`` — when ``_gh`` refreshes, the
        ``_cached_repo`` is dropped and the next access re-fetches via
        the new client. Cheap call (one extra GET) per refresh window.
        """
        if self._cached_repo is None:
            self._cached_repo = self._gh.get_repo(self._repo_full_name)
        return self._cached_repo

    def get_pr_state(self, *, project: str, pr_number: int) -> PRState:
        try:
            pr = self._repo.get_pull(pr_number)
        except GithubException as exc:
            if exc.status == 404:
                raise PRNotFoundError(f"{project}#{pr_number}") from exc
            raise
        return PRState(
            merged=bool(pr.merged),
            mergeable=bool(pr.mergeable),
            ci_passing=(pr.mergeable_state in _CI_PASSING_STATES),
        )

    def merge_spec_pr(self, *, project: str, pr_number: int) -> None:
        pr = self._repo.get_pull(pr_number)
        pr.merge()

    def enqueue_merge_queue(self, *, project: str, pr_number: int) -> None:
        pr = self._repo.get_pull(pr_number)
        # GraphQL mutation — REST API doesn't expose MergeQueue operations.
        mutation = """
            mutation($prId: ID!) {
              enqueuePullRequest(input: {pullRequestId: $prId}) {
                mergeQueueEntry { id }
              }
            }
        """
        requester = self._gh._Github__requester  # type: ignore[attr-defined]
        requester.requestJsonAndCheck(
            "POST", "/graphql",
            input={"query": mutation, "variables": {"prId": pr.node_id}},
        )

    def merge_verdict(self, *, project: str, pr_number: int) -> MergeVerdict:
        pr = self._repo.get_pull(pr_number)
        if pr.merged:
            return MergeVerdict.MERGED
        # GraphQL again: query the mergeQueueEntry for this PR's status.
        query = """
            query($prId: ID!) {
              node(id: $prId) {
                ... on PullRequest {
                  mergeQueueEntry { state }
                }
              }
            }
        """
        requester = self._gh._Github__requester  # type: ignore[attr-defined]
        _, payload = requester.requestJsonAndCheck(
            "POST", "/graphql",
            input={"query": query, "variables": {"prId": pr.node_id}},
        )
        entry = (payload.get("data") or {}).get("node", {}).get("mergeQueueEntry")
        if entry is None:
            return MergeVerdict.PENDING  # not in queue yet
        state = entry.get("state")
        if state == "MERGED":
            return MergeVerdict.MERGED
        if state in ("REJECTED", "FAILED"):
            return MergeVerdict.REJECTED
        return MergeVerdict.PENDING

    def list_open_issues_with_label(
        self, *, project: str, label: str,
    ) -> list[int]:
        issues = self._repo.get_issues(state="open", labels=[label])
        return [issue.number for issue in issues if issue.pull_request is None]

    def write_labels(
        self, *, project: str, issue_number: int, labels: set[str],
    ) -> None:
        # PyGithub's set_labels takes label names as positional args and
        # replaces the existing label set on the issue. Sort for
        # deterministic call shape — useful for log assertions and
        # snapshot tests.
        issue = self._repo.get_issue(issue_number)
        issue.set_labels(*sorted(labels))
