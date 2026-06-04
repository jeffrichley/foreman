"""GraphQL observer — one query per project per poll, returns ProjectSnapshot.

The observer is the only place v3 reads from GitHub. Failures surface as
typed exceptions so the daemon loop can fail-stop with appropriate alerts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from foreman.reconciler.state import IssueState, ProjectSnapshot, PRState


class GHGraphQLClient(Protocol):
    """Thin abstraction so tests can inject a fake.

    Real implementation wraps PyGithub's underlying requester or a direct
    httpx POST to the v4 endpoint. Either way the surface is one method.
    """

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]: ...


class ObserverError(Exception):
    """Base class for observer-side failures."""


class ObserverUnreachable(ObserverError):
    """GitHub did not respond — network error, DNS, timeout."""


class ObserverRateLimited(ObserverError):
    """GitHub returned a rate-limit signal."""


_QUERY = """
query ForemanProjectState($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    issues(
      first: 100,
      states: OPEN,
      filterBy: { labels: [
        "foreman:planning",
        "foreman:plan-approved",
        "foreman:spec-fix",
        "foreman:impl-review",
        "foreman:impl-approved",
        "foreman:impl-fix",
        "foreman:needs-help"
      ] }
    ) {
      nodes {
        number
        title
        body
        state
        updatedAt
        labels(first: 30) { nodes { name } }
        assignees(first: 10) { nodes { login } }
      }
    }
    openPRs: pullRequests(first: 100, states: OPEN) {
      nodes {
        number
        headRefName
        body
        mergeable
        merged
        reviewDecision
        statusCheckRollup { state }
        closingIssuesReferences(first: 10) { nodes { number } }
      }
    }
    recentMergedPRs: pullRequests(
      first: 20,
      states: MERGED,
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      nodes {
        number
        headRefName
        body
        mergeable
        merged
        reviewDecision
        statusCheckRollup { state }
        closingIssuesReferences(first: 10) { nodes { number } }
      }
    }
  }
}
"""


def fetch_project_state(
    *,
    project: str,
    owner: str,
    repo: str,
    gh: GHGraphQLClient,
) -> ProjectSnapshot:
    """One GraphQL call returning the full poll-cycle view of one project."""

    try:
        response = gh.graphql(_QUERY, {"owner": owner, "repo": repo})
    except Exception as exc:
        msg = str(exc).lower()
        if "rate limit" in msg or "api rate limit" in msg:
            raise ObserverRateLimited(str(exc)) from exc
        if isinstance(exc, (ConnectionError, TimeoutError)):
            raise ObserverUnreachable(str(exc)) from exc
        if "timeout" in msg or "getaddrinfo" in msg or "connection" in msg:
            raise ObserverUnreachable(str(exc)) from exc
        raise ObserverError(str(exc)) from exc

    repository = (response.get("data") or {}).get("repository") or {}
    issue_nodes = ((repository.get("issues") or {}).get("nodes")) or []
    open_pr_nodes = ((repository.get("openPRs") or {}).get("nodes")) or []
    merged_pr_nodes = ((repository.get("recentMergedPRs") or {}).get("nodes")) or []

    issues = tuple(_parse_issue(node) for node in issue_nodes)
    prs = tuple(_parse_pr(node) for node in (*open_pr_nodes, *merged_pr_nodes))

    return ProjectSnapshot(
        project=project,
        owner=owner,
        repo=repo,
        issues=issues,
        prs=prs,
        fetched_at=datetime.now(UTC),
    )


def _parse_issue(node: dict[str, Any]) -> IssueState:
    labels = tuple(
        label["name"] for label in (node.get("labels") or {}).get("nodes", [])
    )
    assignees = tuple(
        a["login"] for a in (node.get("assignees") or {}).get("nodes", [])
    )
    updated = _parse_iso(node["updatedAt"])
    return IssueState(
        number=int(node["number"]),
        title=str(node.get("title", "")),
        labels=labels,
        assignees=assignees,
        body=str(node.get("body", "") or ""),
        updated_at=updated,
    )


def _parse_pr(node: dict[str, Any]) -> PRState:
    linked = tuple(
        int(n["number"])
        for n in (node.get("closingIssuesReferences") or {}).get("nodes", [])
    )
    rollup = node.get("statusCheckRollup")
    ci = rollup["state"] if rollup else None
    review_decision = node.get("reviewDecision")  # may be None
    return PRState(
        number=int(node["number"]),
        head_ref=str(node.get("headRefName", "")),
        mergeable=str(node.get("mergeable", "UNKNOWN")),
        ci_status=ci,
        body=str(node.get("body", "") or ""),
        linked_issue_numbers=linked,
        is_merged=bool(node.get("merged", False)),
        review_decision=review_decision,
    )


def _parse_iso(value: str) -> datetime:
    # GitHub returns trailing "Z" — Python's fromisoformat accepts it in 3.11+.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
