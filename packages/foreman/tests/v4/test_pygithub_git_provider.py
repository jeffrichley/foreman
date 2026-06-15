"""PyGithubGitProvider — translates Protocol calls to PyGithub method calls.

This test does NOT hit github.com. It mocks the PyGithub Github client at
the module boundary and asserts the provider issues the expected calls.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from foreman.v4.git_provider import PRNotFoundError, PRState
from foreman.v4.pygithub_git_provider import PyGithubGitProvider


@pytest.fixture()
def mock_repo():
    repo = MagicMock()
    return repo


@pytest.fixture()
def mock_github(mock_repo):
    gh = MagicMock()
    gh.get_repo.return_value = mock_repo
    return gh


def test_get_pr_state_returns_mapped_fields(mock_github, mock_repo):
    mock_pr = MagicMock()
    mock_pr.merged = False
    mock_pr.mergeable = True
    mock_pr.mergeable_state = "clean"
    mock_repo.get_pull.return_value = mock_pr
    provider = PyGithubGitProvider(github=mock_github, repo_full_name="owner/p")
    state = provider.get_pr_state(project="p", pr_number=7)
    assert state == PRState(merged=False, mergeable=True, ci_passing=True)
    mock_repo.get_pull.assert_called_once_with(7)


def test_get_pr_state_missing_raises(mock_github, mock_repo):
    from github import GithubException  # type: ignore[import-not-found]
    mock_repo.get_pull.side_effect = GithubException(status=404, data={}, headers={})
    provider = PyGithubGitProvider(github=mock_github, repo_full_name="owner/p")
    with pytest.raises(PRNotFoundError):
        provider.get_pr_state(project="p", pr_number=999)


def test_list_open_issues_with_label(mock_github, mock_repo):
    issue1 = MagicMock()
    issue1.number = 1
    issue1.pull_request = None
    issue2 = MagicMock()
    issue2.number = 2
    issue2.pull_request = None
    issue_pr = MagicMock()
    issue_pr.number = 3
    issue_pr.pull_request = MagicMock()  # PRs come back from get_issues too
    mock_repo.get_issues.return_value = [issue1, issue2, issue_pr]
    provider = PyGithubGitProvider(github=mock_github, repo_full_name="owner/p")
    issues = provider.list_open_issues_with_label(
        project="p", label="foreman:plan",
    )
    # PRs filtered out:
    assert issues == [1, 2]


def test_merge_spec_pr_calls_merge(mock_github, mock_repo):
    mock_pr = MagicMock()
    mock_repo.get_pull.return_value = mock_pr
    provider = PyGithubGitProvider(github=mock_github, repo_full_name="owner/p")
    provider.merge_spec_pr(project="p", pr_number=5)
    mock_pr.merge.assert_called_once()


def test_enqueue_merge_queue_looks_up_pr(mock_github, mock_repo):
    """MergeQueue enqueue uses GitHub's GraphQL API. The Protocol contract
    is just 'PR has been requested to enter the merge queue.' We assert the
    PR was looked up; the GraphQL call surface is an implementation detail."""
    mock_pr = MagicMock()
    mock_pr.node_id = "PR_node_abc"
    mock_repo.get_pull.return_value = mock_pr
    provider = PyGithubGitProvider(github=mock_github, repo_full_name="owner/p")
    provider.enqueue_merge_queue(project="p", pr_number=11)
    mock_repo.get_pull.assert_called_with(11)


def test_write_labels_calls_set_labels_with_sorted_names(mock_github, mock_repo):
    """``write_labels`` replaces the issue's labels via PyGithub's
    ``set_labels``. Sorting the names makes the call deterministic for
    snapshot/log assertions."""
    mock_issue = MagicMock()
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(github=mock_github, repo_full_name="owner/p")
    provider.write_labels(
        project="p", issue_number=42, labels={"b", "a", "c"},
    )
    mock_repo.get_issue.assert_called_once_with(42)
    mock_issue.set_labels.assert_called_once_with("a", "b", "c")
