"""PyGithubGitProvider — translates Protocol calls to PyGithub method calls.

This test does NOT hit github.com. It mocks the PyGithub Github client at
the module boundary and asserts the provider issues the expected calls.

The provider takes a ``github_factory`` callable (not a static client)
so it can rebuild the underlying ``Github`` when the App installation
token nears expiry. Most tests wrap the existing ``mock_github`` in a
lambda so the call shape is preserved; the refresh-behavior tests at
the bottom exercise the factory + clock seam directly.
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
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    state = provider.get_pr_state(project="p", pr_number=7)
    assert state == PRState(merged=False, mergeable=True, ci_passing=True)
    mock_repo.get_pull.assert_called_once_with(7)


def test_get_pr_state_missing_raises(mock_github, mock_repo):
    from github import GithubException  # type: ignore[import-not-found]
    mock_repo.get_pull.side_effect = GithubException(status=404, data={}, headers={})
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
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
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    issues = provider.list_open_issues_with_label(
        project="p", label="foreman:plan",
    )
    # PRs filtered out:
    assert issues == [1, 2]


def test_merge_spec_pr_calls_merge(mock_github, mock_repo):
    mock_pr = MagicMock()
    mock_repo.get_pull.return_value = mock_pr
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    provider.merge_spec_pr(project="p", pr_number=5)
    mock_pr.merge.assert_called_once()


def test_enqueue_merge_queue_looks_up_pr(mock_github, mock_repo):
    """MergeQueue enqueue uses GitHub's GraphQL API. The Protocol contract
    is just 'PR has been requested to enter the merge queue.' We assert the
    PR was looked up; the GraphQL call surface is an implementation detail."""
    mock_pr = MagicMock()
    mock_pr.node_id = "PR_node_abc"
    mock_repo.get_pull.return_value = mock_pr
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    provider.enqueue_merge_queue(project="p", pr_number=11)
    mock_repo.get_pull.assert_called_with(11)


def test_write_labels_calls_set_labels_with_sorted_names(mock_github, mock_repo):
    """``write_labels`` replaces the issue's labels via PyGithub's
    ``set_labels``. Sorting the names makes the call deterministic for
    snapshot/log assertions."""
    mock_issue = MagicMock()
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    provider.write_labels(
        project="p", issue_number=42, labels={"b", "a", "c"},
    )
    mock_repo.get_issue.assert_called_once_with(42)
    mock_issue.set_labels.assert_called_once_with("a", "b", "c")


# ---------------------------------------------------------------------------
# Token-refresh behavior (Phase 8c.3)
#
# The provider rebuilds its cached ``Github`` client when the cached one is
# older than ``refresh_after_seconds`` (default 50 min). That refresh is the
# only thing keeping the daemon alive past minute ~60, when the App
# installation token expires. These three tests pin the seam down.
# ---------------------------------------------------------------------------


def test_constructor_does_not_invoke_factory():
    """Lazy bootstrap: the factory must NOT be called at construction time.

    If construction eagerly mints a token, every test that instantiates
    the provider (and every CLI command that wires one up) pays the
    GitHub API cost on import. Worse, it removes the daemon's ability
    to defer credential errors until the first real API call. So:
    factory invocation is deferred to first ``_gh`` access.
    """
    def boom() -> object:
        raise AssertionError(
            "github_factory must not be invoked at construction time",
        )
    # Construction must succeed without calling the factory.
    PyGithubGitProvider(
        github_factory=boom, repo_full_name="owner/p",
    )


def test_uses_cached_client_within_refresh_window(mock_github):
    """Inside the refresh window, ``_gh`` returns the SAME cached client.

    Without this, every API call would mint a fresh token + build a
    fresh ``Github`` instance — wasteful and amplifies any
    rate-limit risk on the GitHub App installation-token endpoint.
    """
    current_time = [0.0]
    call_count = [0]

    def factory():
        call_count[0] += 1
        return mock_github

    def clock() -> float:
        return current_time[0]

    provider = PyGithubGitProvider(
        github_factory=factory,
        repo_full_name="owner/p",
        clock=clock,
        refresh_after_seconds=3000.0,
    )

    # First access at t=0 — factory called once.
    first = provider._gh
    assert call_count[0] == 1

    # Advance clock by 1000s (well inside 3000s window). Factory must
    # NOT be called again.
    current_time[0] = 1000.0
    second = provider._gh
    assert call_count[0] == 1
    assert second is first


def test_rebuilds_client_when_cache_expires(mock_github):
    """Past the refresh window, ``_gh`` rebuilds via the factory.

    Also asserts the cached ``Repository`` handle is invalidated on
    refresh — the old ``Repository`` was scoped to the previous
    ``Github`` client's requester, so it would have a stale token.
    """
    current_time = [0.0]
    call_count = [0]

    fresh_clients = [MagicMock(name="github_v1"), MagicMock(name="github_v2")]
    fresh_repos = [MagicMock(name="repo_v1"), MagicMock(name="repo_v2")]
    for gh, repo in zip(fresh_clients, fresh_repos, strict=True):
        gh.get_repo.return_value = repo

    def factory():
        client = fresh_clients[call_count[0]]
        call_count[0] += 1
        return client

    def clock() -> float:
        return current_time[0]

    provider = PyGithubGitProvider(
        github_factory=factory,
        repo_full_name="owner/p",
        clock=clock,
        refresh_after_seconds=3000.0,
    )

    # First access at t=0 — factory called once, returns client #1.
    first_gh = provider._gh
    first_repo = provider._repo
    assert call_count[0] == 1
    assert first_gh is fresh_clients[0]
    assert first_repo is fresh_repos[0]

    # Advance past the refresh window (t=3001 > 3000). Next ``_gh``
    # access rebuilds; the ``_repo`` handle is invalidated and the next
    # ``_repo`` access re-fetches via the new client.
    current_time[0] = 3001.0
    second_gh = provider._gh
    second_repo = provider._repo
    assert call_count[0] == 2
    assert second_gh is fresh_clients[1]
    assert second_repo is fresh_repos[1]
    assert second_gh is not first_gh
    assert second_repo is not first_repo
