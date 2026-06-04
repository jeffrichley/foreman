"""GraphQL observer — one query per project per poll, returns ProjectSnapshot.

The observer is the only place v3 reads from GitHub. Failures surface as
typed exceptions so the daemon loop can fail-stop with appropriate alerts.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Protocol

from foreman.reconciler.state import IssueState, ProjectSnapshot, PRState

# Foreman branches follow ``foreman/issue-<N>`` for spec PRs and
# ``foreman/impl-<N>`` for impl PRs (see ``foreman.branches``). The Planner
# deliberately strips "Closes/Fixes/Resolves" from spec PR bodies so the spec
# PR's merge doesn't auto-close the issue, and the Worker uses
# ``Implements #N`` (not a GitHub auto-link keyword). As a result GitHub's
# ``closingIssuesReferences`` is empty for every real foreman PR in
# production — so the observer also derives the linkage from the head-ref
# convention. ``closingIssuesReferences`` is retained as defense in depth for
# non-foreman PRs that DO use a Closes keyword.
_FOREMAN_BRANCH_RE = re.compile(r"^foreman/(?:issue|impl)-(\d+)$")


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


def _derive_linked_issue_from_head_ref(head_ref: str) -> int | None:
    """Return the issue number encoded in a foreman branch name, else None.

    ``foreman/issue-42`` → 42 (spec PR head)
    ``foreman/impl-42``  → 42 (impl PR head, stacked on spec PR)
    anything else        → None

    Pairs with ``foreman.branches.spec_branch`` and ``impl_branch`` — those
    are the only two shapes foreman roles ever produce, so anything that
    matches is authoritatively linked to that issue.
    """
    match = _FOREMAN_BRANCH_RE.match(head_ref)
    return int(match.group(1)) if match else None


def _parse_pr(node: dict[str, Any]) -> PRState:
    closing_refs = tuple(
        int(n["number"])
        for n in (node.get("closingIssuesReferences") or {}).get("nodes", [])
    )
    head_ref = str(node.get("headRefName", ""))
    head_ref_issue = _derive_linked_issue_from_head_ref(head_ref)

    # Combine both sources, deduping. Head-ref linking is what makes foreman
    # PRs link at all (Planner strips Closes keywords from spec PRs);
    # closingIssuesReferences catches non-foreman PRs that use Closes/Fixes.
    if head_ref_issue is not None and head_ref_issue not in closing_refs:
        linked: tuple[int, ...] = closing_refs + (head_ref_issue,)
    else:
        linked = closing_refs

    rollup = node.get("statusCheckRollup")
    ci = rollup["state"] if rollup else None
    review_decision = node.get("reviewDecision")  # may be None
    return PRState(
        number=int(node["number"]),
        head_ref=head_ref,
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
