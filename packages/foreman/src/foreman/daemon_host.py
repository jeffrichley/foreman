"""GitHubDaemonHost — the orchestrator-bot-backed host adapter for the daemon.

Wraps PyGithub with the foreman-orchestrator-bot's installation token. Used
by the daemon for all "host" operations: label management, polling search,
PR merging, issue closing. These actions don't belong to any per-role bot;
giving them a dedicated identity ensures every Foreman action has a proper
audit trail (no human PAT exposed).

The host holds an :class:`~foreman.identity.IdentityRegistry` reference and
asks it for a fresh orchestrator-bot client on every API call. The
registry handles caching + the 5-minute-pre-expiry refresh, so the
daemon survives past the 1-hour installation-token TTL without the host
holding any stale state of its own (issue #44).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from foreman.identity import IdentityRegistry

if TYPE_CHECKING:
    from github import GithubException


def _is_merge_method_disallowed(exc: GithubException) -> bool:
    """Return True iff ``exc`` is GitHub's 405 for a disabled merge method.

    GitHub returns 405 with messages like ``"Merge commits are not
    allowed on this repository."`` / ``"Squash merging is not allowed
    on this repository."`` / ``"Rebase merging is not allowed on this
    repository."`` when the operator-requested merge method is disabled
    in repo settings. Other 405 responses (e.g. PR not in a mergeable
    state) should NOT be caught — they need to bubble up.
    """
    if exc.status != 405:
        return False
    message = (exc.data or {}).get("message", "") if hasattr(exc, "data") else ""
    return "is not allowed on this repository" in message or "are not allowed on this repository" in message


@dataclass(frozen=True)
class IssueSnapshot:
    """The subset of GitHub issue data the daemon's poller cares about."""

    number: int
    labels: list[str]
    updated_at: datetime


class GitHubDaemonHost:
    """Adapter exposing the daemon's required host operations via PyGithub."""

    def __init__(self, *, identity_registry: IdentityRegistry) -> None:
        self._registry = identity_registry

    def search_foreman_labeled_issues(self, repo: str) -> list[IssueSnapshot]:
        """Return all open issues on the repo carrying any foreman:* label.

        PyGithub doesn't accept ``label:foreman:*`` in ``get_issues``;
        we filter client-side after fetching open issues. This is a
        v1 simplification — for repos with hundreds of open issues we
        can switch to the search API.
        """
        gh = self._registry.get_orchestrator_client()
        repo_obj = gh.get_repo(repo)
        result: list[IssueSnapshot] = []
        for issue in repo_obj.get_issues(state="open"):
            label_names = [lbl.name for lbl in issue.labels]
            if not any(name.startswith("foreman:") for name in label_names):
                continue
            result.append(
                IssueSnapshot(
                    number=issue.number,
                    labels=label_names,
                    updated_at=issue.updated_at,
                )
            )
        return result

    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        gh = self._registry.get_orchestrator_client()
        repo_obj = gh.get_repo(repo)
        issue = repo_obj.get_issue(issue_number)
        issue.add_to_labels(label)

    def remove_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        gh = self._registry.get_orchestrator_client()
        repo_obj = gh.get_repo(repo)
        issue = repo_obj.get_issue(issue_number)
        issue.remove_from_labels(label)

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        gh = self._registry.get_orchestrator_client()
        repo_obj = gh.get_repo(repo)
        issue = repo_obj.get_issue(issue_number)
        issue.create_comment(body)

    def get_issue_labels(self, repo: str, issue_number: int) -> list[str]:
        gh = self._registry.get_orchestrator_client()
        repo_obj = gh.get_repo(repo)
        issue = repo_obj.get_issue(issue_number)
        return [lbl.name for lbl in issue.labels]

    def close_issue(self, repo: str, issue_number: int) -> None:
        gh = self._registry.get_orchestrator_client()
        repo_obj = gh.get_repo(repo)
        issue = repo_obj.get_issue(issue_number)
        issue.edit(state="closed")

    def find_pr_for_branch(self, repo: str, branch: str) -> int | None:
        """Return the open PR number whose head branch == ``branch``, or None."""
        gh = self._registry.get_orchestrator_client()
        repo_obj = gh.get_repo(repo)
        owner = repo.split("/")[0]
        for pr in repo_obj.get_pulls(state="open", head=f"{owner}:{branch}"):
            if pr.head.ref == branch:
                return int(pr.number)
        return None

    def merge_pull_request(
        self, repo: str, pr_number: int, *, merge_method: str = "merge"
    ) -> None:
        """Merge the PR using GitHub's merge API.

        Default strategy: merge commit. Squash and rebase are options;
        v1 uses merge commits to preserve the agent's commit history
        for audit trail.

        Fallback chain (issue #130): if the target repo disables a merge
        method via its GitHub settings, GitHub returns a 405 with a
        message identifying the disallowed method. We fall through to
        the next allowed method (merge -> squash -> rebase) rather than
        crashing the dispatch loop. This matters because foreman has no
        out-of-band knowledge of each target repo's settings; the API
        is the source of truth and the fallback degrades gracefully.

        Raises the original exception if ALL three methods are
        disallowed (an unusual repo configuration that needs operator
        attention) or if the failure isn't a 405-merge-method error.
        """
        from github import GithubException

        gh = self._registry.get_orchestrator_client()
        repo_obj = gh.get_repo(repo)
        pr = repo_obj.get_pull(pr_number)

        # Ordered chain: try the operator-requested method first, then
        # the others as fallbacks. Removing the requested method from
        # the tail avoids retrying it.
        chain = [merge_method] + [m for m in ("merge", "squash", "rebase") if m != merge_method]
        last_exc: GithubException | None = None
        for method in chain:
            try:
                pr.merge(merge_method=method)
                return
            except GithubException as exc:
                if not _is_merge_method_disallowed(exc):
                    raise
                last_exc = exc
                continue
        # All methods refused. Re-raise the last exception so the
        # operator sees a real GitHub error message rather than a
        # synthesized one.
        if last_exc is not None:
            raise last_exc

    def get_pr_base_ref(self, repo: str, pr_number: int) -> str:
        """Return the PR's current ``base.ref`` (the branch it merges into)."""
        gh = self._registry.get_orchestrator_client()
        repo_obj = gh.get_repo(repo)
        pr = repo_obj.get_pull(pr_number)
        return pr.base.ref

    def is_pr_merged_for_branch(self, repo: str, branch: str) -> bool:
        """Return True iff a closed, merged PR exists whose head branch == ``branch``.

        Used by the daemon's impl-PR retarget step to decide whether the
        spec PR has already landed (see ``daemon_runners.merge_impl_pr``
        and issue #62 for the ghost-merge failure mode this guards
        against). GitHub deletes the head branch on merge when
        auto-delete-branch is on, but the closed PR's metadata persists,
        so ``repo.get_pulls(state="closed", head=...)`` still finds it.
        """
        gh = self._registry.get_orchestrator_client()
        repo_obj = gh.get_repo(repo)
        owner = repo.split("/")[0]
        for pr in repo_obj.get_pulls(state="closed", head=f"{owner}:{branch}"):
            if pr.head.ref == branch and pr.merged:
                return True
        return False

    def retarget_pr_base(self, repo: str, pr_number: int, new_base: str) -> None:
        """Retarget an open PR's base branch via the GitHub API.

        Wraps PyGithub's ``pr.edit(base=new_base)`` — corresponds to
        ``PATCH /repos/{owner}/{repo}/pulls/{N}`` with
        ``{"base": new_base}``. Used by ``daemon_runners.merge_impl_pr``
        to point an impl PR at the default branch before merge so the
        squash commit lands on a reachable ref (issue #62).
        """
        gh = self._registry.get_orchestrator_client()
        repo_obj = gh.get_repo(repo)
        pr = repo_obj.get_pull(pr_number)
        pr.edit(base=new_base)

    def get_default_branch(self, repo: str) -> str:
        """Return the repo's default branch name (typically ``main``)."""
        gh = self._registry.get_orchestrator_client()
        repo_obj = gh.get_repo(repo)
        return repo_obj.default_branch
