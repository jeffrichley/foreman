"""Unit tests for the shared :mod:`foreman.roles._pr_lookup` helper.

``find_open_pr_by_head_branch`` is a thin wrapper over
``repo.get_pulls(state="open", head=f"{owner}:{branch}")``. These tests
fake a PyGithub ``repo`` (a :class:`~unittest.mock.MagicMock` with a
``get_pulls`` side-effect keyed on the head qualifier — mirroring the
mock pattern in ``tests/v4/roles/test_worker_core.py``) and assert:

- a branch with an open PR returns that PR (number-bearing), and
- a branch with no open PR returns ``None``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from foreman.roles._pr_lookup import find_open_pr_by_head_branch


def _build_repo(*, owner: str, matched_branch: str, pr_number: int) -> MagicMock:
    """Fake a PyGithub repo whose ``get_pulls`` matches one branch only.

    ``get_pulls(state="open", head=f"{owner}:{matched_branch}")`` yields a
    single PR (number ``pr_number``); every other head qualifier yields
    nothing.
    """
    matched_pr = MagicMock()
    matched_pr.number = pr_number

    def _get_pulls_side_effect(*, state: str, head: str) -> list[MagicMock]:
        assert state == "open"
        if head == f"{owner}:{matched_branch}":
            return [matched_pr]
        return []

    repo = MagicMock()
    repo.get_pulls.side_effect = _get_pulls_side_effect
    return repo


def test_returns_pr_when_open_pr_exists_on_head_branch() -> None:
    owner = "testowner"
    branch = "foreman/issue-341"
    repo = _build_repo(owner=owner, matched_branch=branch, pr_number=42)

    found = find_open_pr_by_head_branch(repo, owner=owner, branch=branch)

    assert found is not None
    assert found.number == 42
    repo.get_pulls.assert_called_once_with(
        state="open", head=f"{owner}:{branch}"
    )


def test_returns_none_when_no_open_pr_on_head_branch() -> None:
    owner = "testowner"
    repo = _build_repo(
        owner=owner, matched_branch="foreman/issue-341", pr_number=42
    )

    found = find_open_pr_by_head_branch(
        repo, owner=owner, branch="foreman/issue-999"
    )

    assert found is None
    repo.get_pulls.assert_called_once_with(
        state="open", head=f"{owner}:foreman/issue-999"
    )
