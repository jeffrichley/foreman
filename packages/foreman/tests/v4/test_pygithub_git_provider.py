"""PyGithubGitProvider — translates Protocol calls to PyGithub method calls.

This test does NOT hit github.com. It patches the module-level ``Github``
constructor and uses a mock identity whose ``get_role_token`` return value
is controlled by the test. Token-refresh tests assert that a new ``Github``
client is built iff the registry returns a different token string — rather
than relying on a clock seam.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, call, patch

import pytest

from foreman.git_host import CommentRef
from foreman.v4.git_provider import PRNotFoundError, PRState
from foreman.v4.pygithub_git_provider import PyGithubGitProvider, _resolve_merge_method


@pytest.fixture()
def mock_repo():
    repo = MagicMock()
    return repo


@pytest.fixture()
def mock_identity():
    identity = MagicMock()
    identity.get_role_token.return_value = "token_v1"
    return identity


@pytest.fixture()
def mock_github(mock_repo):
    gh = MagicMock()
    gh.get_repo.return_value = mock_repo
    with patch("foreman.v4.pygithub_git_provider.Github", return_value=gh):
        yield gh


def test_get_pr_state_returns_mapped_fields(mock_github, mock_repo, mock_identity):
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
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    state = provider.get_pr_state(project="p", pr_number=7)
    assert state == PRState(
        merged=False,
        mergeable=True,
        ci_passing=True,
        base_ref="main",
        mergeable_state="clean",
    )
    mock_repo.get_pull.assert_called_once_with(7)


def test_get_pr_state_missing_raises(mock_github, mock_repo, mock_identity):
    from github import GithubException  # type: ignore[import-not-found]

    mock_repo.get_pull.side_effect = GithubException(status=404, data={}, headers={})
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    with pytest.raises(PRNotFoundError):
        provider.get_pr_state(project="p", pr_number=999)


def test_list_open_issues_with_label(mock_github, mock_repo, mock_identity):
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
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    issues = provider.list_open_issues_with_label(
        project="p",
        label="foreman:plan",
    )
    # PRs filtered out:
    assert issues == [1, 2]


def test_merge_pr_calls_merge(mock_github, mock_repo, mock_identity):
    """``merge_pr`` calls ``pr.merge()`` via the REST endpoint —
    same shape for spec PRs (SpecReviewState) and impl PRs
    (MergingState). Phase 8d.19 dropped the MergeQueue GraphQL path
    so this is the only merge entry point now."""
    mock_pr = MagicMock()
    mock_repo.get_pull.return_value = mock_pr
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    provider.merge_pr(project="p", pr_number=5)
    mock_pr.merge.assert_called_once_with(merge_method="squash")


def test_update_branch_calls_pr_update_branch(mock_github, mock_repo, mock_identity):
    """foreman#416: ``update_branch`` maps to ``pr.update_branch()``
    (PUT .../update-branch). BehindBranchHealer calls this to self-heal a
    base-advanced PR instead of 405-ing on a merge attempt."""
    mock_pr = MagicMock()
    mock_repo.get_pull.return_value = mock_pr
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    provider.update_branch(project="p", pr_number=5)
    mock_repo.get_pull.assert_called_once_with(5)
    mock_pr.update_branch.assert_called_once_with()


def test_add_labels_calls_add_to_labels_with_sorted_names(mock_github, mock_repo, mock_identity):
    """``add_labels`` adds without touching other labels via PyGithub's
    ``add_to_labels`` (REST endpoint is additive, not replace). Sorted
    arg order makes the call deterministic for snapshot/log assertions."""
    mock_issue = MagicMock()
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    provider.add_labels(
        project="p",
        issue_number=42,
        labels={"b", "a", "c"},
    )
    mock_repo.get_issue.assert_called_once_with(42)
    mock_issue.add_to_labels.assert_called_once_with("a", "b", "c")
    # Never falls back to set_labels (the destructive replace shape).
    mock_issue.set_labels.assert_not_called()


def test_add_labels_empty_set_is_no_op(mock_github, mock_repo, mock_identity):
    """PyGithub's ``add_to_labels`` raises with no args; the provider
    short-circuits on an empty set so callers can pass {} safely."""
    mock_issue = MagicMock()
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    provider.add_labels(project="p", issue_number=42, labels=set())
    mock_repo.get_issue.assert_not_called()
    mock_issue.add_to_labels.assert_not_called()


def test_remove_labels_calls_remove_from_labels_per_label(mock_github, mock_repo, mock_identity):
    """REST API has no bulk-remove endpoint, so PyGithub's
    ``remove_from_labels`` takes one label at a time. The provider
    calls it once per label in sorted order."""
    mock_issue = MagicMock()
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    provider.remove_labels(
        project="p",
        issue_number=42,
        labels={"b", "a"},
    )
    mock_repo.get_issue.assert_called_once_with(42)
    # One call per label, sorted ascending.
    assert mock_issue.remove_from_labels.call_args_list == [
        call("a"),
        call("b"),
    ]


def test_remove_labels_swallows_unknown_object_exception(mock_github, mock_repo, mock_identity):
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
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    # Must not raise — even though the first label triggers 404.
    provider.remove_labels(
        project="p",
        issue_number=42,
        labels={"missing", "present"},
    )
    # Both labels were attempted; the 404 didn't short-circuit the loop.
    assert mock_issue.remove_from_labels.call_count == 2


def test_remove_labels_swallows_github_exception_404(mock_github, mock_repo, mock_identity):
    """Removing a label not on the issue can surface as a bare
    GithubException(status=404) ("Label does not exist") instead of
    UnknownObjectException, depending on PyGithub's internal path. That
    404 must also be swallowed — otherwise it crashes the observer that
    strips the trigger label on state entry and fails the ticket."""
    from github import GithubException  # type: ignore[import-not-found]

    mock_issue = MagicMock()
    mock_repo.get_issue.return_value = mock_issue
    # First call raises a generic 404 (label not on issue), second succeeds.
    mock_issue.remove_from_labels.side_effect = [
        GithubException(status=404, data={"message": "Label does not exist"}, headers={}),
        None,
    ]
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    # Must not raise — the generic 404 is idempotent-no-op like UnknownObjectException.
    provider.remove_labels(
        project="p",
        issue_number=42,
        labels={"missing", "present"},
    )
    # Both labels were attempted; the 404 didn't short-circuit the loop.
    assert mock_issue.remove_from_labels.call_count == 2


def test_remove_labels_reraises_non_404_github_exception(mock_github, mock_repo, mock_identity):
    """A non-404 GithubException (e.g. 500) is a real failure and must
    propagate — only the label-not-on-issue 404 is swallowed."""
    from github import GithubException  # type: ignore[import-not-found]

    mock_issue = MagicMock()
    mock_repo.get_issue.return_value = mock_issue
    mock_issue.remove_from_labels.side_effect = GithubException(
        status=500, data={"message": "Server Error"}, headers={}
    )
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    with pytest.raises(GithubException):
        provider.remove_labels(project="p", issue_number=42, labels={"boom"})


def test_remove_labels_empty_set_is_no_op(mock_github, mock_repo, mock_identity):
    """Empty set short-circuits before the get_issue API call."""
    mock_issue = MagicMock()
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    provider.remove_labels(project="p", issue_number=42, labels=set())
    mock_repo.get_issue.assert_not_called()
    mock_issue.remove_from_labels.assert_not_called()


def test_close_issue_calls_edit_when_open(mock_github, mock_repo, mock_identity):
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
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    provider.close_issue(project="p", issue_number=23)
    mock_repo.get_issue.assert_called_once_with(23)
    mock_issue.edit.assert_called_once_with(state="closed")


def test_close_issue_skips_edit_when_already_closed(mock_github, mock_repo, mock_identity):
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
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    provider.close_issue(project="p", issue_number=23)
    mock_repo.get_issue.assert_called_once_with(23)
    mock_issue.edit.assert_not_called()


def test_get_issue_state_returns_current_state(mock_github, mock_repo, mock_identity):
    """foreman#409: ``get_issue_state`` reads ``issue.state`` from GitHub so the
    reset guard can detect a closed issue before re-arming ``foreman:plan``."""
    mock_issue = MagicMock()
    mock_issue.state = "closed"
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    assert provider.get_issue_state(project="p", issue_number=23) == "closed"
    mock_repo.get_issue.assert_called_once_with(23)


def test_reopen_issue_calls_edit_when_closed(mock_github, mock_repo, mock_identity):
    """foreman#409: on a closed issue, ``reopen_issue`` calls
    ``issue.edit(state="open")`` so ``--force-reopen`` reopens before reset."""
    mock_issue = MagicMock()
    mock_issue.state = "closed"
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    provider.reopen_issue(project="p", issue_number=23)
    mock_repo.get_issue.assert_called_once_with(23)
    mock_issue.edit.assert_called_once_with(state="open")


def test_reopen_issue_skips_edit_when_already_open(mock_github, mock_repo, mock_identity):
    """foreman#409: on an already-open issue, ``reopen_issue`` does NOT call
    ``edit`` — the idempotency guard mirrors ``close_issue``."""
    mock_issue = MagicMock()
    mock_issue.state = "open"
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    provider.reopen_issue(project="p", issue_number=23)
    mock_repo.get_issue.assert_called_once_with(23)
    mock_issue.edit.assert_not_called()


# ---------------------------------------------------------------------------
# Token-refresh behavior
#
# The provider rebuilds its cached ``Github`` client when the registry
# returns a different token string. These tests pin the seam via a mock
# identity whose get_role_token return value is controlled by the test.
# ---------------------------------------------------------------------------


def test_constructor_does_not_invoke_identity(mock_identity):
    """Lazy bootstrap: the identity must NOT be queried at construction time.

    If construction eagerly calls get_role_token, every test that
    instantiates the provider pays the (potentially network) cost of token
    resolution. Construction must succeed without querying the registry.
    """
    PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    mock_identity.get_role_token.assert_not_called()


def test_gh_does_not_rebuild_when_registry_returns_same_token(mock_identity):
    """Constant token → Github() constructed exactly once across 100 accesses.

    Without this, every _gh access would rebuild the Github client on a
    constant token — wasteful and incorrect.
    """
    with patch("foreman.v4.pygithub_git_provider.Github") as MockGithub:
        mock_gh_instance = MagicMock()
        MockGithub.return_value = mock_gh_instance
        mock_identity.get_role_token.return_value = "token_v1"

        provider = PyGithubGitProvider(
            identity=mock_identity,
            role="orchestrator",
            repo_full_name="owner/p",
        )
        for _ in range(100):
            _ = provider._gh

        assert MockGithub.call_count == 1


def test_gh_rebuilds_when_registry_token_changes(mock_identity):
    """Different token → two distinct Github instances returned.

    When the registry hands back a new token string, _gh must build a
    new Github client and return it.
    """
    gh_v1 = MagicMock(name="v1")
    gh_v2 = MagicMock(name="v2")

    with patch("foreman.v4.pygithub_git_provider.Github", side_effect=[gh_v1, gh_v2]):
        provider = PyGithubGitProvider(
            identity=mock_identity,
            role="orchestrator",
            repo_full_name="owner/p",
        )

        mock_identity.get_role_token.return_value = "token_v1"
        first = provider._gh
        assert first is gh_v1

        mock_identity.get_role_token.return_value = "token_v2"
        second = provider._gh
        assert second is gh_v2

        assert first is not second


def test_repo_access_alone_triggers_gh_rebuild_on_token_change(mock_identity):
    """The Poller's hot path only accesses ``_repo``, never ``_gh`` directly.

    Access _repo multiple times with the same token (one Github() call);
    change the token via the mock identity; next _repo access must return a
    new Repository instance and Github must have been called twice total.
    """
    repo_v1 = MagicMock(name="repo_v1")
    repo_v2 = MagicMock(name="repo_v2")
    gh_v1 = MagicMock(name="github_v1")
    gh_v2 = MagicMock(name="github_v2")
    gh_v1.get_repo.return_value = repo_v1
    gh_v2.get_repo.return_value = repo_v2

    with patch(
        "foreman.v4.pygithub_git_provider.Github",
        side_effect=[gh_v1, gh_v2],
    ) as MockGithub:
        mock_identity.get_role_token.return_value = "token_v1"
        provider = PyGithubGitProvider(
            identity=mock_identity,
            role="orchestrator",
            repo_full_name="owner/p",
        )

        # Simulate the Poller's access pattern: only `_repo`, never `_gh`.
        first_repo = provider._repo
        assert first_repo is repo_v1
        assert MockGithub.call_count == 1

        # Multiple ticks with same token — same cached Repository, no rebuild.
        same_repo = provider._repo
        assert same_repo is repo_v1
        assert MockGithub.call_count == 1

        # Change token → next _repo access (no intermediate _gh access) must
        # rebuild Github AND re-fetch Repository.
        mock_identity.get_role_token.return_value = "token_v2"
        second_repo = provider._repo
        assert MockGithub.call_count == 2, (
            "MockGithub must have been called twice — first for token_v1, "
            "then for token_v2. A count of 1 means _repo short-circuited on "
            "the cached Repository handle and _gh's token-equality check never "
            "fired. Daemon would 401-die once the old token expires."
        )
        assert second_repo is repo_v2
        assert second_repo is not first_repo


def test_get_issue_comments_uses_refreshed_client_on_token_change(mock_identity):
    """get_issue_comments goes through _gh → triggers token rebuild on change.

    Two clients (client_v1, client_v2) returned by patched Github via
    side_effect. First get_issue_comments call while token is "token_v1"
    uses client_v1; change token to "token_v2"; second call uses client_v2.
    """
    client_v1 = MagicMock(name="github_v1")
    client_v2 = MagicMock(name="github_v2")
    repo_v1 = MagicMock(name="repo_v1")
    repo_v2 = MagicMock(name="repo_v2")
    client_v1.get_repo.return_value = repo_v1
    client_v2.get_repo.return_value = repo_v2

    fake_issue_v1 = MagicMock()
    fake_issue_v1.get_comments.return_value = []
    fake_issue_v2 = MagicMock()
    fake_issue_v2.get_comments.return_value = []
    repo_v1.get_issue.return_value = fake_issue_v1
    repo_v2.get_issue.return_value = fake_issue_v2

    with patch(
        "foreman.v4.pygithub_git_provider.Github",
        side_effect=[client_v1, client_v2],
    ):
        mock_identity.get_role_token.return_value = "token_v1"
        provider = PyGithubGitProvider(
            identity=mock_identity,
            role="orchestrator",
            repo_full_name="org/repo",
        )

        # First call: token_v1, uses client_v1.
        provider.get_issue_comments(project="ignored", issue_number=10)
        repo_v1.get_issue.assert_called_once_with(10)
        repo_v2.get_issue.assert_not_called()

        # Change token → next call must rebuild client and use client_v2.
        mock_identity.get_role_token.return_value = "token_v2"
        provider.get_issue_comments(project="ignored", issue_number=10)
        repo_v2.get_issue.assert_called_once_with(10)


# ---------------------------------------------------------------------------
# Reset-CLI methods (Task 5)
#
# delete_branch, close_pr, find_open_pr_by_head_branch back the foreman
# reset CLI command. delete_branch and close_pr swallow 404/422 because
# reset is idempotent — "already gone" is the success state.
# ---------------------------------------------------------------------------


def test_delete_branch_calls_git_ref_delete(mock_github, mock_repo, mock_identity):
    """delete_branch resolves heads/<name> then calls .delete()."""
    fake_ref = MagicMock()
    mock_repo.get_git_ref.return_value = fake_ref
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="org/repo",
    )
    provider.delete_branch(project="ignored", branch_name="foreman/issue-1")
    mock_repo.get_git_ref.assert_called_once_with("heads/foreman/issue-1")
    fake_ref.delete.assert_called_once()


def test_delete_branch_swallows_404(mock_github, mock_repo, mock_identity):
    """A 404 (ref doesn't exist) must not raise — reset relies on this."""
    from github import GithubException  # type: ignore[import-not-found]

    mock_repo.get_git_ref.side_effect = GithubException(
        status=404,
        data={"message": "Not Found"},
        headers={},
    )
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="org/repo",
    )
    # Must NOT raise.
    provider.delete_branch(project="ignored", branch_name="foreman/issue-1")


def test_close_pr_calls_pull_edit_state_closed(mock_github, mock_repo, mock_identity):
    fake_pr = MagicMock()
    mock_repo.get_pull.return_value = fake_pr
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="org/repo",
    )
    provider.close_pr(project="ignored", pr_number=19)
    mock_repo.get_pull.assert_called_once_with(19)
    fake_pr.edit.assert_called_once_with(state="closed")


def test_close_pr_swallows_already_closed(mock_github, mock_repo, mock_identity):
    """422 (PR already closed) must not raise."""
    from github import GithubException  # type: ignore[import-not-found]

    fake_pr = MagicMock()
    fake_pr.edit.side_effect = GithubException(
        status=422,
        data={
            "message": "Validation Failed",
            "errors": [{"resource": "PullRequest", "code": "invalid"}],
        },
        headers={},
    )
    mock_repo.get_pull.return_value = fake_pr
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="org/repo",
    )
    provider.close_pr(project="ignored", pr_number=19)


def test_find_open_pr_by_head_branch_uses_get_pulls_with_head_filter(
    mock_github,
    mock_repo,
    mock_identity,
):
    fake_pr = MagicMock(number=19)
    mock_repo.owner.login = "org"
    mock_repo.get_pulls.return_value = [fake_pr]
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="org/repo",
    )
    result = provider.find_open_pr_by_head_branch(
        project="ignored",
        branch_name="foreman/issue-180",
    )
    assert result == 19
    mock_repo.get_pulls.assert_called_once_with(
        state="open",
        head="org:foreman/issue-180",
    )


def test_find_open_pr_by_head_branch_returns_none_on_empty(
    mock_github,
    mock_repo,
    mock_identity,
):
    mock_repo.owner.login = "org"
    mock_repo.get_pulls.return_value = []
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="org/repo",
    )
    assert (
        provider.find_open_pr_by_head_branch(
            project="ignored",
            branch_name="foreman/issue-180",
        )
        is None
    )


# ---------------------------------------------------------------------------
# _resolve_merge_method unit tests (issue #399)
# ---------------------------------------------------------------------------


def test_resolve_merge_method_squash_only():
    """Squash-only repo (only allow_squash_merge=True) → 'squash'."""
    repo = MagicMock()
    repo.allow_squash_merge = True
    repo.allow_rebase_merge = False
    repo.allow_merge_commit = False
    assert _resolve_merge_method(repo) == "squash"


def test_resolve_merge_method_all_allowed_no_preferred_returns_squash():
    """All three methods allowed, no preferred → 'squash' (first in order)."""
    repo = MagicMock()
    repo.allow_squash_merge = True
    repo.allow_rebase_merge = True
    repo.allow_merge_commit = True
    assert _resolve_merge_method(repo) == "squash"


def test_resolve_merge_method_none_allowed_raises():
    """No allowed methods → ValueError with a descriptive message."""
    repo = MagicMock()
    repo.allow_squash_merge = False
    repo.allow_rebase_merge = False
    repo.allow_merge_commit = False
    with pytest.raises(ValueError, match="no allowed merge method"):
        _resolve_merge_method(repo)


def test_resolve_merge_method_preferred_allowed_returns_preferred():
    """preferred='rebase', rebase allowed → 'rebase' (not overridden)."""
    repo = MagicMock()
    repo.allow_squash_merge = True
    repo.allow_rebase_merge = True
    repo.allow_merge_commit = True
    assert _resolve_merge_method(repo, preferred="rebase") == "rebase"


def test_resolve_merge_method_preferred_disallowed_falls_back():
    """preferred='rebase', rebase NOT allowed → 'squash' (first allowed)."""
    repo = MagicMock()
    repo.allow_squash_merge = True
    repo.allow_rebase_merge = False
    repo.allow_merge_commit = True
    assert _resolve_merge_method(repo, preferred="rebase") == "squash"


def test_get_issue_labels_returns_label_names(mock_github, mock_repo, mock_identity):
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
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="org/repo",
    )
    labels = provider.get_issue_labels(project="ignored", issue_number=180)
    assert labels == {"foreman:state-failed", "bug"}
    mock_repo.get_issue.assert_called_once_with(180)


# ---------------------------------------------------------------------------
# Issue-comment methods (sub-request 2 of issue #410)
#
# get_issue_comments and post_issue_comment go through self._repo → self._gh,
# which means they inherit the token-equality check automatically. The tests
# below pin the basic call shape.
# ---------------------------------------------------------------------------


def test_get_issue_comments_returns_sorted_comment_refs(mock_github, mock_repo, mock_identity):
    """get_issue_comments maps PyGithub IssueComment objects to CommentRef.

    Maps: author_login ← user.login, posted_at ← created_at, body ← body.
    Returns them in the order PyGithub yields them (chronological from GitHub).
    """
    fake_comment_a = MagicMock()
    fake_comment_a.user.login = "alice"
    fake_comment_a.created_at = dt.datetime(2026, 6, 20, 10, 0, 0, tzinfo=dt.UTC)
    fake_comment_a.body = "first comment"

    fake_comment_b = MagicMock()
    fake_comment_b.user.login = "bob"
    fake_comment_b.created_at = dt.datetime(2026, 6, 20, 11, 0, 0, tzinfo=dt.UTC)
    fake_comment_b.body = "second comment"

    fake_issue = MagicMock()
    fake_issue.get_comments.return_value = [fake_comment_a, fake_comment_b]
    mock_repo.get_issue.return_value = fake_issue

    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="org/repo",
    )
    result = provider.get_issue_comments(project="ignored", issue_number=55)

    assert result == [
        CommentRef(
            author_login="alice",
            posted_at=dt.datetime(2026, 6, 20, 10, 0, 0, tzinfo=dt.UTC),
            body="first comment",
        ),
        CommentRef(
            author_login="bob",
            posted_at=dt.datetime(2026, 6, 20, 11, 0, 0, tzinfo=dt.UTC),
            body="second comment",
        ),
    ]
    mock_repo.get_issue.assert_called_once_with(55)
    fake_issue.get_comments.assert_called_once_with()


def test_post_issue_comment_calls_create_comment(mock_github, mock_repo, mock_identity):
    """post_issue_comment calls issue.create_comment(body) via PyGithub."""
    fake_issue = MagicMock()
    mock_repo.get_issue.return_value = fake_issue

    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="org/repo",
    )
    provider.post_issue_comment(project="ignored", issue_number=99, body="test body")

    mock_repo.get_issue.assert_called_once_with(99)
    fake_issue.create_comment.assert_called_once_with("test body")


# ---------------------------------------------------------------------------
# get_issue_state_reason (foreman#524)
# ---------------------------------------------------------------------------


def test_get_issue_state_reason_reads_state_reason(mock_github, mock_repo, mock_identity):
    """foreman#524: ``get_issue_state_reason`` reads ``issue.state_reason`` from
    GitHub so the dependency reconciler can distinguish closed-as-completed from
    closed-as-not-planned (only the former satisfies a dependency)."""
    mock_issue = MagicMock()
    mock_issue.state_reason = "completed"
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    assert provider.get_issue_state_reason(project="p", issue_number=42) == "completed"
    mock_repo.get_issue.assert_called_once_with(42)


def test_get_issue_state_reason_returns_none_when_github_returns_none(
    mock_github, mock_repo, mock_identity
):
    """An open issue has ``state_reason=None`` on GitHub; the provider passes it
    through so callers can treat None as "not completed"."""
    mock_issue = MagicMock()
    mock_issue.state_reason = None
    mock_repo.get_issue.return_value = mock_issue
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    assert provider.get_issue_state_reason(project="p", issue_number=7) is None


# ---------------------------------------------------------------------------
# read_blocked_by (foreman#524)
# ---------------------------------------------------------------------------


def test_read_blocked_by_maps_response_to_issue_numbers(mock_github, mock_repo, mock_identity):
    """foreman#524: ``read_blocked_by`` calls the native dependencies endpoint via the
    PyGithub requester and maps the returned issue objects to a list of issue numbers."""
    mock_repo.url = "https://api.github.com/repos/owner/p"
    mock_repo.requester.requestJsonAndCheck.return_value = (
        {},
        [{"number": 290, "title": "some dep"}, {"number": 285, "title": "another dep"}],
    )
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    result = provider.read_blocked_by(project="p", issue_number=291)
    assert result == [290, 285]
    # The endpoint is issue-scoped: /repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by.
    # A repo-scoped URL (missing the /issues/{n} segment) 404s for every ticket and
    # crash-loops the daemon (regression guard — that shipped once).
    mock_repo.requester.requestJsonAndCheck.assert_called_once_with(
        "GET",
        "https://api.github.com/repos/owner/p/issues/291/dependencies/blocked_by",
    )


def test_read_blocked_by_returns_empty_list_when_no_blockers(mock_github, mock_repo, mock_identity):
    """An issue with no blockers returns an empty list from the endpoint."""
    mock_repo.url = "https://api.github.com/repos/owner/p"
    mock_repo.requester.requestJsonAndCheck.return_value = ({}, [])
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    result = provider.read_blocked_by(project="p", issue_number=1)
    assert result == []


def test_read_blocked_by_returns_empty_list_on_404(mock_github, mock_repo, mock_identity):
    """A 404 from the dependencies endpoint (unavailable for the repo, or issue not
    found) must degrade to an empty list — a data-plane read must never propagate and
    crash the poller/daemon. Regression guard for the crash-loop this caused."""
    from github import UnknownObjectException

    mock_repo.url = "https://api.github.com/repos/owner/p"
    mock_repo.requester.requestJsonAndCheck.side_effect = UnknownObjectException(
        status=404, data={"message": "Not Found"}, headers={}
    )
    provider = PyGithubGitProvider(
        identity=mock_identity,
        role="orchestrator",
        repo_full_name="owner/p",
    )
    result = provider.read_blocked_by(project="p", issue_number=291)
    assert result == []
