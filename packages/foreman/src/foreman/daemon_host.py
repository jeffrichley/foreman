"""GitHubDaemonHost — the orchestrator-bot-backed host adapter for the daemon.

Wraps PyGithub with the foreman-orchestrator-bot's installation token. Used
by the daemon for all "host" operations: label management, polling search,
PR merging, issue closing. These actions don't belong to any per-role bot;
giving them a dedicated identity ensures every Foreman action has a proper
audit trail (no human PAT exposed).

Token resolution and Github client construction happen at daemon startup
in ``cli.py``; this module just wraps an already-authenticated client.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from github import Github

from foreman.auth import mint_installation_token


@dataclass(frozen=True)
class IssueSnapshot:
    """The subset of GitHub issue data the daemon's poller cares about."""

    number: int
    labels: list[str]
    updated_at: datetime


def build_orchestrator_github_client(
    *, app_id: int, private_key_path: Path | str, repo_slug_for_install: str
) -> Github:
    """Mint an installation token for the orchestrator bot and return a
    PyGithub client authenticated with it.

    ``repo_slug_for_install`` is any repo the App is installed on — used
    to look up the installation id. The resulting token is valid for the
    whole installation (i.e., every repo where the App is installed).
    """
    token = mint_installation_token(app_id, private_key_path, repo_slug_for_install)
    return Github(token.token)


class GitHubDaemonHost:
    """Adapter exposing the daemon's required host operations via PyGithub."""

    def __init__(self, *, github_client: Github) -> None:
        self._gh = github_client

    def search_foreman_labeled_issues(self, repo: str) -> list[IssueSnapshot]:
        """Return all open issues on the repo carrying any foreman:* label.

        PyGithub doesn't accept ``label:foreman:*`` in ``get_issues``;
        we filter client-side after fetching open issues. This is a
        v1 simplification — for repos with hundreds of open issues we
        can switch to the search API.
        """
        repo_obj = self._gh.get_repo(repo)
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
        repo_obj = self._gh.get_repo(repo)
        issue = repo_obj.get_issue(issue_number)
        issue.add_to_labels(label)

    def remove_issue_label(self, repo: str, issue_number: int, label: str) -> None:
        repo_obj = self._gh.get_repo(repo)
        issue = repo_obj.get_issue(issue_number)
        issue.remove_from_labels(label)

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> None:
        repo_obj = self._gh.get_repo(repo)
        issue = repo_obj.get_issue(issue_number)
        issue.create_comment(body)

    def get_issue_labels(self, repo: str, issue_number: int) -> list[str]:
        repo_obj = self._gh.get_repo(repo)
        issue = repo_obj.get_issue(issue_number)
        return [lbl.name for lbl in issue.labels]

    def close_issue(self, repo: str, issue_number: int) -> None:
        repo_obj = self._gh.get_repo(repo)
        issue = repo_obj.get_issue(issue_number)
        issue.edit(state="closed")

    def find_pr_for_branch(self, repo: str, branch: str) -> int | None:
        """Return the open PR number whose head branch == ``branch``, or None."""
        repo_obj = self._gh.get_repo(repo)
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
        """
        repo_obj = self._gh.get_repo(repo)
        pr = repo_obj.get_pull(pr_number)
        pr.merge(merge_method=merge_method)

    def get_pr_base_ref(self, repo: str, pr_number: int) -> str:
        """Return the PR's current ``base.ref`` (the branch it merges into)."""
        repo_obj = self._gh.get_repo(repo)
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
        repo_obj = self._gh.get_repo(repo)
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
        repo_obj = self._gh.get_repo(repo)
        pr = repo_obj.get_pull(pr_number)
        pr.edit(base=new_base)

    def get_default_branch(self, repo: str) -> str:
        """Return the repo's default branch name (typically ``main``)."""
        repo_obj = self._gh.get_repo(repo)
        return repo_obj.default_branch
