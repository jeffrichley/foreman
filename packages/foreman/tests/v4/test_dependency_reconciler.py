"""Tests for the dependency reconciler — pure logic, no side effects."""

from foreman.v4.dependency_reconciler import compute_unmet_dependencies
from foreman.v4.git_provider import FakeGitProvider


def test_no_blocked_by_returns_empty() -> None:
    p = FakeGitProvider()
    assert compute_unmet_dependencies(project="ac", issue_number=291, provider=p) == []


def test_open_dep_is_unmet() -> None:
    p = FakeGitProvider()
    p.set_blocked_by(project="ac", issue_number=291, blocked_by=[290])
    # 290 open → state_reason None → unmet
    assert compute_unmet_dependencies(project="ac", issue_number=291, provider=p) == [290]


def test_completed_dep_is_filtered_out() -> None:
    p = FakeGitProvider()
    p.set_blocked_by(project="ac", issue_number=291, blocked_by=[290])
    p.set_issue_state_reason(project="ac", issue_number=290, reason="completed")
    assert compute_unmet_dependencies(project="ac", issue_number=291, provider=p) == []


def test_not_planned_dep_stays_unmet() -> None:
    p = FakeGitProvider()
    p.set_blocked_by(project="ac", issue_number=291, blocked_by=[290])
    p.set_issue_state_reason(project="ac", issue_number=290, reason="not_planned")
    assert compute_unmet_dependencies(project="ac", issue_number=291, provider=p) == [290]
