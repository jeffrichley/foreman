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

from unittest.mock import MagicMock, call

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
    # foreman#357: PyGithub's pr.base is a GitRef object; .ref is the
    # branch name. MergingState's base-ref guard compares this against
    # ProjectConfig.dev_base_branch before merging.
    mock_pr.base.ref = "main"
    mock_repo.get_pull.return_value = mock_pr
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    state = provider.get_pr_state(project="p", pr_number=7)
    assert state == PRState(
        merged=False, mergeable=True, ci_passing=True, base_ref="main",
    )
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


def test_merge_pr_calls_merge(mock_github, mock_repo):
    """``merge_pr`` calls ``pr.merge()`` via the REST endpoint —
    same shape for spec PRs (SpecReviewState) and impl PRs
    (MergingState). Phase 8d.19 dropped the MergeQueue GraphQL path
    so this is the only merge entry point now."""
    mock_pr = MagicMock()
    mock_repo.get_pull.return_value = mock_pr
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    provider.merge_pr(project="p", pr_number=5)
    mock_pr.merge.assert_called_once()


def test_add_labels_calls_add_to_labels_with_sorted_names(mock_github, mock_repo):
    """``add_labels`` adds without touching other labels via PyGithub's
    ``add_to_labels`` (REST endpoint is additive, not replace). Sorted
    arg order makes the call deterministic for snapshot/log assertions."""
    mock_issue = MagicMock()
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    provider.add_labels(
        project="p", issue_number=42, labels={"b", "a", "c"},
    )
    mock_repo.get_issue.assert_called_once_with(42)
    mock_issue.add_to_labels.assert_called_once_with("a", "b", "c")
    # Never falls back to set_labels (the destructive replace shape).
    mock_issue.set_labels.assert_not_called()


def test_add_labels_empty_set_is_no_op(mock_github, mock_repo):
    """PyGithub's ``add_to_labels`` raises with no args; the provider
    short-circuits on an empty set so callers can pass {} safely."""
    mock_issue = MagicMock()
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    provider.add_labels(project="p", issue_number=42, labels=set())
    mock_repo.get_issue.assert_not_called()
    mock_issue.add_to_labels.assert_not_called()


def test_remove_labels_calls_remove_from_labels_per_label(mock_github, mock_repo):
    """REST API has no bulk-remove endpoint, so PyGithub's
    ``remove_from_labels`` takes one label at a time. The provider
    calls it once per label in sorted order."""
    mock_issue = MagicMock()
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    provider.remove_labels(
        project="p", issue_number=42, labels={"b", "a"},
    )
    mock_repo.get_issue.assert_called_once_with(42)
    # One call per label, sorted ascending.
    assert mock_issue.remove_from_labels.call_args_list == [
        call("a"),
        call("b"),
    ]


def test_remove_labels_swallows_unknown_object_exception(mock_github, mock_repo):
    """Removing a label not on the issue triggers a DELETE → 404 →
    PyGithub's UnknownObjectException. The Protocol contract is
    idempotent on missing labels, so we swallow and continue with the
    next label."""
    from github import UnknownObjectException  # type: ignore[import-not-found]

    mock_issue = MagicMock()
    mock_repo.get_issue.return_value = mock_issue
    # First call raises (label not on issue), second call succeeds.
    mock_issue.remove_from_labels.side_effect = [
        UnknownObjectException(status=404, data={}, headers={}),
        None,
    ]
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    # Must not raise — even though the first label triggers 404.
    provider.remove_labels(
        project="p", issue_number=42, labels={"missing", "present"},
    )
    # Both labels were attempted; the 404 didn't short-circuit the loop.
    assert mock_issue.remove_from_labels.call_count == 2


def test_remove_labels_empty_set_is_no_op(mock_github, mock_repo):
    """Empty set short-circuits before the get_issue API call."""
    mock_issue = MagicMock()
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    provider.remove_labels(project="p", issue_number=42, labels=set())
    mock_repo.get_issue.assert_not_called()
    mock_issue.remove_from_labels.assert_not_called()


def test_close_issue_calls_edit_when_open(mock_github, mock_repo):
    """When the issue is still open, ``close_issue`` calls
    ``issue.edit(state="closed")`` — the PyGithub idiom for closing.

    Phase 8d.20: MergingState closes the originating GitHub issue after
    a successful impl-PR merge so the loop's terminal state propagates
    back to the issue tracker.
    """
    mock_issue = MagicMock()
    mock_issue.state = "open"
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    provider.close_issue(project="p", issue_number=23)
    mock_repo.get_issue.assert_called_once_with(23)
    mock_issue.edit.assert_called_once_with(state="closed")


def test_close_issue_skips_edit_when_already_closed(mock_github, mock_repo):
    """When the issue is already closed, ``close_issue`` does NOT call
    ``edit`` — the guard avoids wasting a PATCH request and side-steps
    the (small) race-window risk where a fresh edit could reopen a
    just-closed issue. GitHub's API treats already-closed as a no-op,
    so the contract is "idempotent at the caller level too."
    """
    mock_issue = MagicMock()
    mock_issue.state = "closed"
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github, repo_full_name="owner/p",
    )
    provider.close_issue(project="p", issue_number=23)
    mock_repo.get_issue.assert_called_once_with(23)
    mock_issue.edit.assert_not_called()


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


def test_repo_access_alone_triggers_gh_rebuild_past_window():
    """The Poller's hot path only accesses ``_repo``, never ``_gh`` directly.

    Earlier tests pre-2026-06-15 invoked ``provider._gh`` explicitly at
    the post-expiry timestamp to demonstrate the rebuild. But the
    realistic daemon code path is ``provider._repo.get_issues(...)`` —
    the Poller never touches ``_gh`` at all. If ``_repo``'s cached
    Repository handle short-circuits before ``_gh`` is invoked, the
    time-based rebuild check inside ``_gh`` never fires, the cached
    ``Github`` client clings to its expiring installation token, and
    the daemon 401-dies at minute 60. This test exercises the realistic
    pattern (only ``_repo`` accesses across the window) and asserts the
    rebuild fires anyway. Regression guard for the bug confirmed in the
    2026-06-15 dogfood (foreman v4 daemon died at 60min wall-clock,
    despite 8c.3 + 8d.13 thresholds being arithmetically correct).
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

    # Simulate the Poller's access pattern: only `_repo`, never `_gh`.
    first_repo = provider._repo
    assert call_count[0] == 1
    assert first_repo is fresh_repos[0]

    # Many ticks within the window — same cached Repository, no rebuild.
    for tick_time in (30.0, 60.0, 1500.0, 2970.0, 3000.0):
        current_time[0] = tick_time
        same_repo = provider._repo
        assert same_repo is fresh_repos[0]
        assert call_count[0] == 1

    # Past the window: the very next `_repo` access (no intermediate
    # `_gh` access) must rebuild Github AND re-fetch Repository.
    current_time[0] = 3001.0
    rebuilt_repo = provider._repo
    assert call_count[0] == 2, (
        "Factory must have been called twice — first at t=0, then again "
        "when the Poller's _repo access crossed t=3001 past the window. "
        "Got call_count=1 means _repo short-circuited on the cached "
        "Repository handle and _gh's rebuild check never fired. "
        "Daemon would 401-die at minute 60."
    )
    assert rebuilt_repo is fresh_repos[1]
    assert rebuilt_repo is not first_repo


# ---------------------------------------------------------------------------
# Reset-CLI methods (Task 5)
#
# delete_branch, close_pr, find_open_pr_by_head_branch back the foreman
# reset CLI command. delete_branch and close_pr swallow 404/422 because
# reset is idempotent — "already gone" is the success state.
# ---------------------------------------------------------------------------


def test_delete_branch_calls_git_ref_delete(mock_github, mock_repo):
    """delete_branch resolves heads/<name> then calls .delete()."""
    fake_ref = MagicMock()
    mock_repo.get_git_ref.return_value = fake_ref
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github,
        repo_full_name="org/repo",
    )
    provider.delete_branch(project="ignored", branch_name="foreman/issue-1")
    mock_repo.get_git_ref.assert_called_once_with("heads/foreman/issue-1")
    fake_ref.delete.assert_called_once()


def test_delete_branch_swallows_404(mock_github, mock_repo):
    """A 404 (ref doesn't exist) must not raise — reset relies on this."""
    from github import GithubException  # type: ignore[import-not-found]

    mock_repo.get_git_ref.side_effect = GithubException(
        status=404, data={"message": "Not Found"}, headers={},
    )
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github,
        repo_full_name="org/repo",
    )
    # Must NOT raise.
    provider.delete_branch(project="ignored", branch_name="foreman/issue-1")


def test_close_pr_calls_pull_edit_state_closed(mock_github, mock_repo):
    fake_pr = MagicMock()
    mock_repo.get_pull.return_value = fake_pr
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github,
        repo_full_name="org/repo",
    )
    provider.close_pr(project="ignored", pr_number=19)
    mock_repo.get_pull.assert_called_once_with(19)
    fake_pr.edit.assert_called_once_with(state="closed")


def test_close_pr_swallows_already_closed(mock_github, mock_repo):
    """422 (PR already closed) must not raise."""
    from github import GithubException  # type: ignore[import-not-found]

    fake_pr = MagicMock()
    fake_pr.edit.side_effect = GithubException(
        status=422,
        data={"message": "Validation Failed", "errors": [
            {"resource": "PullRequest", "code": "invalid"}]},
        headers={},
    )
    mock_repo.get_pull.return_value = fake_pr
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github,
        repo_full_name="org/repo",
    )
    provider.close_pr(project="ignored", pr_number=19)


def test_find_open_pr_by_head_branch_uses_get_pulls_with_head_filter(
    mock_github, mock_repo,
):
    fake_pr = MagicMock(number=19)
    mock_repo.owner.login = "org"
    mock_repo.get_pulls.return_value = [fake_pr]
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github,
        repo_full_name="org/repo",
    )
    result = provider.find_open_pr_by_head_branch(
        project="ignored", branch_name="foreman/issue-180",
    )
    assert result == 19
    mock_repo.get_pulls.assert_called_once_with(
        state="open", head="org:foreman/issue-180",
    )


def test_find_open_pr_by_head_branch_returns_none_on_empty(
    mock_github, mock_repo,
):
    mock_repo.owner.login = "org"
    mock_repo.get_pulls.return_value = []
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github,
        repo_full_name="org/repo",
    )
    assert provider.find_open_pr_by_head_branch(
        project="ignored", branch_name="foreman/issue-180",
    ) is None


def test_get_issue_labels_returns_label_names(mock_github, mock_repo):
    """``get_issue_labels`` reads the names off the issue's label objects.

    Reset CLI's discovery phase uses this to enumerate ``foreman:*``
    labels currently on the issue. PyGithub returns label objects
    (each with a ``.name``); the provider unpacks them into a set.
    """
    fake_label_a = MagicMock()
    fake_label_a.name = "foreman:state-failed"
    fake_label_b = MagicMock()
    fake_label_b.name = "bug"
    fake_issue = MagicMock(labels=[fake_label_a, fake_label_b])
    mock_repo.get_issue.return_value = fake_issue
    provider = PyGithubGitProvider(
        github_factory=lambda: mock_github,
        repo_full_name="org/repo",
    )
    labels = provider.get_issue_labels(project="ignored", issue_number=180)
    assert labels == {"foreman:state-failed", "bug"}
    mock_repo.get_issue.assert_called_once_with(180)
