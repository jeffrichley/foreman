"""PyGithubGitProvider — production GitProvider backed by PyGithub.

Tests use FakeGitProvider (Task 3.2); production uses this. The seam
matches the Protocol from foreman.v4.git_provider.
"""

from __future__ import annotations

from github import Github, GithubException

from foreman.v4.git_provider import MergeVerdict, PRNotFoundError, PRState

_CI_PASSING_STATES = frozenset({"clean", "unstable"})


class PyGithubGitProvider:
    def __init__(self, *, github: Github, repo_full_name: str) -> None:
        self._gh = github
        self._repo = github.get_repo(repo_full_name)

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
