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
        return self.response or {
            "data": {
                "repository": {
                    "issues": {"nodes": []},
                    "openPRs": {"nodes": []},
                    "recentMergedPRs": {"nodes": []},
                }
            }
        }


def _gh_response_with(
    *,
    issues: list[dict],
    prs: list[dict],
    merged_prs: list[dict] | None = None,
) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "issues": {"nodes": issues},
                "openPRs": {"nodes": prs},
                "recentMergedPRs": {"nodes": merged_prs or []},
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


def test_observer_query_includes_plan_entry_label() -> None:
    """foreman#171: ``foreman:plan`` is the documented entry label —
    operators set it when filing a ticket. Without it in
    ``filterBy.labels`` the daemon never receives the issue in its
    snapshot, so the ``advance_label_to_planning`` rule (which exists
    to auto-transition this label) can never fire and the ticket stalls
    silently."""
    from foreman.reconciler.observer import _QUERY
    assert '"foreman:plan"' in _QUERY


def test_observer_query_includes_spec_fix_label() -> None:
    # spec-fix issues should appear in the observer poll so the daemon can
    # surface them for human follow-up (no auto-rerun, just visibility).
    from foreman.reconciler.observer import _QUERY
    assert "foreman:spec-fix" in _QUERY


def test_observer_query_includes_hold_and_failed_labels() -> None:
    """Pass 2 MEDIUM: tickets carrying ONLY ``foreman:hold`` (operator-paused)
    or ONLY ``foreman:failed`` (terminal) must still be visible to the
    reconciler so safety rules + dashboard surfacing can fire. Without these
    in ``filterBy.labels`` the GraphQL query silently omits them and they
    fall off the daemon's radar."""
    from foreman.reconciler.observer import _QUERY
    assert "foreman:hold" in _QUERY
    assert "foreman:failed" in _QUERY


def test_observer_fetches_recently_merged_prs_for_lagging_label_recovery() -> None:
    """Lagging-label safety rules need to see merged PRs. The observer must
    include recently-merged PRs in the snapshot so those rules can fire in
    production (not just in synthetic test contexts)."""

    class FakeGH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((query, variables))
            # Verify the query asks for merged PRs too.
            assert "MERGED" in query, "observer must fetch merged PRs"

            return {
                "data": {
                    "repository": {
                        "issues": {"nodes": []},
                        "openPRs": {
                            "nodes": [
                                {
                                    "number": 1,
                                    "headRefName": "feat/open",
                                    "body": "",
                                    "mergeable": "MERGEABLE",
                                    "merged": False,
                                    "statusCheckRollup": {"state": "SUCCESS"},
                                    "closingIssuesReferences": {"nodes": []},
                                    "reviewDecision": None,
                                }
                            ]
                        },
                        "recentMergedPRs": {
                            "nodes": [
                                {
                                    "number": 2,
                                    "headRefName": "feat/merged",
                                    "body": "",
                                    # post-merge value isn't meaningful but field is present
                                    "mergeable": "MERGEABLE",
                                    "merged": True,
                                    "statusCheckRollup": {"state": "SUCCESS"},
                                    "closingIssuesReferences": {"nodes": []},
                                    "reviewDecision": "APPROVED",
                                }
                            ]
                        },
                    }
                }
            }

    gh = FakeGH()
    snap = fetch_project_state(
        project="foreman", owner="jeffrichley", repo="foreman", gh=gh,
    )
    # Both PRs should be present.
    assert len(snap.prs) == 2
    pr_numbers = {pr.number for pr in snap.prs}
    assert pr_numbers == {1, 2}
    # The merged one's is_merged should be True so lagging-label rules fire.
    merged = next(p for p in snap.prs if p.number == 2)
    assert merged.is_merged is True
    # And the open one should remain not-merged.
    open_pr = next(p for p in snap.prs if p.number == 1)
    assert open_pr.is_merged is False


def test_observer_query_uses_aliased_pr_connections() -> None:
    """The observer combines two pullRequests connections via GraphQL aliases
    (openPRs + recentMergedPRs) so the parser can pull both lists out of one
    request."""
    from foreman.reconciler.observer import _QUERY
    assert "openPRs:" in _QUERY
    assert "recentMergedPRs:" in _QUERY
    assert "states: OPEN" in _QUERY
    assert "states: MERGED" in _QUERY
    # Recent-merged window must be bounded so old PRs don't pollute the snapshot.
    assert "UPDATED_AT" in _QUERY


def test_observer_links_spec_pr_to_issue_via_head_ref_when_closing_refs_empty() -> None:
    """Foreman spec PRs strip Closes keywords; observer must link via head-ref.

    Planner deliberately removes "Closes/Fixes/Resolves" from spec PR bodies
    (roles/planner.py) so the spec PR's merge doesn't auto-close the issue.
    That makes ``closingIssuesReferences`` empty in production. The observer
    must derive the linkage from the ``foreman/issue-<N>`` head-ref
    convention instead.
    """
    pr_payload = {
        "number": 99,
        "headRefName": "foreman/issue-42",
        "body": "Implements #42",
        "mergeable": "MERGEABLE",
        "merged": False,
        "statusCheckRollup": {"state": "SUCCESS"},
        "closingIssuesReferences": {"nodes": []},  # EMPTY — Planner stripped
        "reviewDecision": None,
    }
    client = _FakeGHClient(response=_gh_response_with(issues=[], prs=[pr_payload]))
    snap = fetch_project_state(
        project="foreman", owner="jeffrichley", repo="foreman", gh=client,
    )
    assert len(snap.prs) == 1
    assert 42 in snap.prs[0].linked_issue_numbers


def test_observer_links_impl_pr_to_issue_via_head_ref_when_closing_refs_empty() -> None:
    """Foreman impl PRs use ``Implements #N`` (not a GH auto-link keyword);
    observer must link via the ``foreman/impl-<N>`` head-ref convention."""
    pr_payload = {
        "number": 100,
        "headRefName": "foreman/impl-42",
        "body": "Implements #42",
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
    assert len(snap.prs) == 1
    assert 42 in snap.prs[0].linked_issue_numbers


def test_observer_combines_head_ref_and_closing_refs() -> None:
    """A non-foreman PR using a Closes keyword still links via
    closingIssuesReferences — head-ref linking is defense in depth, not a
    replacement."""
    pr_payload = {
        "number": 101,
        "headRefName": "feature/manual-pr",
        "body": "Closes #99",
        "mergeable": "MERGEABLE",
        "merged": False,
        "statusCheckRollup": {"state": "SUCCESS"},
        "closingIssuesReferences": {"nodes": [{"number": 99}]},
        "reviewDecision": None,
    }
    client = _FakeGHClient(response=_gh_response_with(issues=[], prs=[pr_payload]))
    snap = fetch_project_state(
        project="foreman", owner="jeffrichley", repo="foreman", gh=client,
    )
    assert 99 in snap.prs[0].linked_issue_numbers


def test_observer_does_not_double_link_when_both_sources_agree() -> None:
    """If head-ref points to N AND closingIssuesReferences contains N (e.g.,
    an operator manually appended ``Closes #N`` to a foreman-shaped branch),
    N appears exactly once in linked_issue_numbers."""
    pr_payload = {
        "number": 99,
        "headRefName": "foreman/issue-42",
        "body": "Closes #42",
        "mergeable": "MERGEABLE",
        "merged": False,
        "statusCheckRollup": {"state": "SUCCESS"},
        "closingIssuesReferences": {"nodes": [{"number": 42}]},
        "reviewDecision": None,
    }
    client = _FakeGHClient(response=_gh_response_with(issues=[], prs=[pr_payload]))
    snap = fetch_project_state(
        project="foreman", owner="jeffrichley", repo="foreman", gh=client,
    )
    linked = snap.prs[0].linked_issue_numbers
    assert linked.count(42) == 1
