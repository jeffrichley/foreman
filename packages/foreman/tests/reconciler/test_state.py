"""Tests for v3 state dataclasses — ProjectSnapshot, IssueState, PRState."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foreman.reconciler.state import IssueState, PRState, ProjectSnapshot


def test_issue_state_is_frozen() -> None:
    issue = IssueState(
        number=143,
        title="Daemon stuck on planning",
        labels=("foreman:planning",),
        assignees=(),
        body="",
        updated_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    with pytest.raises(Exception):
        issue.number = 999  # type: ignore[misc]


def test_pr_state_is_frozen() -> None:
    pr = PRState(
        number=144,
        head_ref="spec-143-fix",
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="Implements #143",
        linked_issue_numbers=(143,),
        is_merged=False,
    )
    with pytest.raises(Exception):
        pr.number = 999  # type: ignore[misc]


def test_project_snapshot_finds_issue_by_number() -> None:
    issue = IssueState(
        number=143,
        title="x",
        labels=("foreman:planning",),
        assignees=(),
        body="",
        updated_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    snap = ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(issue,),
        prs=(),
        fetched_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    assert snap.find_issue(143) is issue
    assert snap.find_issue(999) is None


def test_project_snapshot_returns_prs_linked_to_issue() -> None:
    linked_pr = PRState(
        number=144,
        head_ref="spec-143-fix",
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="Implements #143",
        linked_issue_numbers=(143,),
        is_merged=False,
    )
    unrelated_pr = PRState(
        number=200,
        head_ref="other",
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="",
        linked_issue_numbers=(),
        is_merged=False,
    )
    snap = ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(),
        prs=(linked_pr, unrelated_pr),
        fetched_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    linked = snap.prs_for_issue(143)
    assert linked == (linked_pr,)


def test_ticket_id_format() -> None:
    snap = ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(),
        prs=(),
        fetched_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    assert snap.ticket_id_for(143) == "jeffrichley/foreman#143"
