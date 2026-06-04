"""Tests for the v3 GraphQL observer — query shape, response parsing, errors."""

from __future__ import annotations

from typing import Any

import pytest

from foreman.reconciler.observer import (
    ObserverRateLimited,
    ObserverUnreachable,
    fetch_project_state,
)


class _FakeGHClient:
    def __init__(self, *, response: dict[str, Any] | None = None, raise_with: Exception | None = None) -> None:
        self.response = response
        self.raise_with = raise_with
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, variables))
        if self.raise_with:
            raise self.raise_with
        return self.response or {"data": {"repository": {"issues": {"nodes": []}, "pullRequests": {"nodes": []}}}}


def _gh_response_with(*, issues: list[dict], prs: list[dict]) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "issues": {"nodes": issues},
                "pullRequests": {"nodes": prs},
            }
        }
    }


def test_fetch_project_state_returns_empty_snapshot_for_empty_response() -> None:
    client = _FakeGHClient(response=_gh_response_with(issues=[], prs=[]))
    snap = fetch_project_state(
        project="foreman", owner="jeffrichley", repo="foreman", gh=client,
    )
    assert snap.project == "foreman"
    assert snap.owner == "jeffrichley"
    assert snap.repo == "foreman"
    assert snap.issues == ()
    assert snap.prs == ()
    assert snap.fetched_at.tzinfo is not None


def test_fetch_project_state_parses_issue_fields() -> None:
    issue_payload = {
        "number": 143,
        "title": "Daemon stuck on planning",
        "body": "details",
        "state": "OPEN",
        "updatedAt": "2026-06-03T15:00:00Z",
        "labels": {"nodes": [{"name": "foreman:planning"}, {"name": "good first issue"}]},
        "assignees": {"nodes": [{"login": "wrenrichley"}]},
    }
    client = _FakeGHClient(response=_gh_response_with(issues=[issue_payload], prs=[]))
    snap = fetch_project_state(project="foreman", owner="jeffrichley", repo="foreman", gh=client)

    assert len(snap.issues) == 1
    iss = snap.issues[0]
    assert iss.number == 143
    assert iss.title == "Daemon stuck on planning"
    assert iss.labels == ("foreman:planning", "good first issue")
    assert iss.assignees == ("wrenrichley",)


def test_fetch_project_state_parses_pr_with_linked_issue() -> None:
    pr_payload = {
        "number": 144,
        "headRefName": "spec-143-fix",
        "body": "Implements #143",
        "mergeable": "MERGEABLE",
        "merged": False,
        "statusCheckRollup": {"state": "SUCCESS"},
        "closingIssuesReferences": {"nodes": [{"number": 143}]},
    }
    client = _FakeGHClient(response=_gh_response_with(issues=[], prs=[pr_payload]))
    snap = fetch_project_state(project="foreman", owner="jeffrichley", repo="foreman", gh=client)

    assert len(snap.prs) == 1
    pr = snap.prs[0]
    assert pr.number == 144
    assert pr.head_ref == "spec-143-fix"
    assert pr.mergeable == "MERGEABLE"
    assert pr.ci_status == "SUCCESS"
    assert pr.linked_issue_numbers == (143,)
    assert pr.is_merged is False


def test_fetch_project_state_handles_null_status_check_rollup() -> None:
    pr_payload = {
        "number": 144,
        "headRefName": "x",
        "body": "",
        "mergeable": "UNKNOWN",
        "merged": False,
        "statusCheckRollup": None,
        "closingIssuesReferences": {"nodes": []},
    }
    client = _FakeGHClient(response=_gh_response_with(issues=[], prs=[pr_payload]))
    snap = fetch_project_state(project="foreman", owner="jeffrichley", repo="foreman", gh=client)
    assert snap.prs[0].ci_status is None


def test_observer_rate_limited_raises_typed_error() -> None:
    class _GQLError(Exception):
        pass
    err = _GQLError("API rate limit exceeded for installation")
    client = _FakeGHClient(raise_with=err)
    with pytest.raises(ObserverRateLimited):
        fetch_project_state(project="foreman", owner="jeffrichley", repo="foreman", gh=client)


def test_observer_network_error_raises_typed_error() -> None:
    err = ConnectionError("getaddrinfo failed")
    client = _FakeGHClient(raise_with=err)
    with pytest.raises(ObserverUnreachable):
        fetch_project_state(project="foreman", owner="jeffrichley", repo="foreman", gh=client)


def test_observer_query_includes_only_foreman_labeled_issues() -> None:
    client = _FakeGHClient(response=_gh_response_with(issues=[], prs=[]))
    fetch_project_state(project="foreman", owner="jeffrichley", repo="foreman", gh=client)
    query, variables = client.calls[0]
    assert "foreman:" in query
    assert variables == {"owner": "jeffrichley", "repo": "foreman"}


def test_fetch_project_state_parses_review_decision_approved() -> None:
    pr_payload = {
        "number": 42,
        "headRefName": "feat/x",
        "body": "",
        "mergeable": "MERGEABLE",
        "merged": False,
        "statusCheckRollup": {"state": "SUCCESS"},
        "closingIssuesReferences": {"nodes": []},
        "reviewDecision": "APPROVED",
    }
    client = _FakeGHClient(response=_gh_response_with(issues=[], prs=[pr_payload]))
    snap = fetch_project_state(
        project="foreman", owner="jeffrichley", repo="foreman", gh=client,
    )
    query, _variables = client.calls[0]
    assert "reviewDecision" in query
    assert len(snap.prs) == 1
    assert snap.prs[0].review_decision == "APPROVED"


def test_fetch_project_state_handles_null_review_decision() -> None:
    pr_payload = {
        "number": 42,
        "headRefName": "feat/x",
        "body": "",
        "mergeable": "MERGEABLE",
        "merged": False,
        "statusCheckRollup": {"state": "SUCCESS"},
        "closingIssuesReferences": {"nodes": []},
        "reviewDecision": None,
    }
    client = _FakeGHClient(response=_gh_response_with(issues=[], prs=[pr_payload]))
    snap = fetch_project_state(
        project="foreman", owner="jeffrichley", repo="foreman", gh=client,
    )
    assert snap.prs[0].review_decision is None
