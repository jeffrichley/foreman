"""Tests for the foreman.branches single-source-of-truth helpers.

These tests assert literal-string equality on purpose. The whole reason
the module exists is to pin the branch-name convention; routing the
expected value back through the helper-under-test would defeat the
test.
"""

from __future__ import annotations

import pytest

from foreman.branches import impl_branch, spec_branch


@pytest.mark.parametrize(
    ("issue_number", "expected"),
    [
        (0, "foreman/issue-0"),
        (1, "foreman/issue-1"),
        (46, "foreman/issue-46"),
        (99999, "foreman/issue-99999"),
    ],
)
def test_spec_branch_returns_literal(issue_number: int, expected: str) -> None:
    assert spec_branch(issue_number) == expected


@pytest.mark.parametrize(
    ("issue_number", "expected"),
    [
        (0, "foreman/impl-0"),
        (1, "foreman/impl-1"),
        (46, "foreman/impl-46"),
        (99999, "foreman/impl-99999"),
    ],
)
def test_impl_branch_returns_literal(issue_number: int, expected: str) -> None:
    assert impl_branch(issue_number) == expected
