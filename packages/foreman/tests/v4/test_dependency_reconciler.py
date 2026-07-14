"""Tests for the dependency reconciler — pure logic, no side effects."""

from foreman.v4.dependency_reconciler import compute_unmet_dependencies, find_cycles
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


# ---------------------------------------------------------------------------
# find_cycles — pure DFS cycle detector
# ---------------------------------------------------------------------------


def test_find_cycles_mutual_block_returns_cycle() -> None:
    """Two tickets each blocked_by the other → one 2-node cycle."""
    assert find_cycles({1: [2], 2: [1]}) == [[1, 2]]


def test_find_cycles_no_cycle_returns_empty() -> None:
    """One-directional dependency → no cycle."""
    assert find_cycles({1: [2], 2: []}) == []


def test_find_cycles_self_loop() -> None:
    """A ticket blocked_by itself → 1-node cycle."""
    assert find_cycles({1: [1]}) == [[1]]


def test_find_cycles_three_node_cycle() -> None:
    """1 → 2 → 3 → 1 forms a single 3-node cycle."""
    result = find_cycles({1: [2], 2: [3], 3: [1]})
    assert result == [[1, 2, 3]]


def test_find_cycles_isolated_node_no_cycle() -> None:
    """A node with no edges contributes no cycle."""
    assert find_cycles({1: [], 2: []}) == []


def test_find_cycles_two_independent_cycles() -> None:
    """Two separate 2-node cycles are both returned, outer list sorted."""
    result = find_cycles({1: [2], 2: [1], 3: [4], 4: [3]})
    assert result == [[1, 2], [3, 4]]
