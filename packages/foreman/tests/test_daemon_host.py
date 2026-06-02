"""Tests for GitHubDaemonHost — the orchestrator-bot-backed host adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock

from foreman.daemon_host import GitHubDaemonHost


@dataclass
class _FakeLabel:
    name: str


@dataclass
class _FakeIssue:
    number: int
    labels: list[_FakeLabel]
    updated_at: object  # datetime

    def add_to_labels(self, name: str) -> None:
        self.labels.append(_FakeLabel(name=name))

    def remove_from_labels(self, name: str) -> None:
        self.labels = [lbl for lbl in self.labels if lbl.name != name]

    def create_comment(self, body: str) -> None:
        self._last_comment = body

    def edit(self, state: str | None = None) -> None:
        self.state = state


@dataclass
class _FakePR:
    number: int
    head_branch: str
    base_ref: str = "main"
    merged: bool = False
    last_edit_kwargs: dict = field(default_factory=dict)

    @property
    def base(self) -> object:
        return SimpleNamespace(ref=self.base_ref)

    @property
    def head(self) -> object:
        return SimpleNamespace(ref=self.head_branch)

    def merge(self, commit_message: str | None = None, merge_method: str = "merge") -> None:
        self.merged = True
        self.merge_method = merge_method

    def edit(self, **kwargs: object) -> None:
        self.last_edit_kwargs.update(kwargs)


def _make_host_with_repo(repo) -> GitHubDaemonHost:
    """Build a host whose Github client returns the given repo for any slug.

    The host now takes an IdentityRegistry rather than a raw Github client;
    we stand up a MagicMock registry whose ``get_orchestrator_client`` returns
    the canned Github mock. Existing tests that only exercise the host's
    public API surface continue to work unchanged.
    """
    gh_client = MagicMock()
    gh_client.get_repo = MagicMock(return_value=repo)
    registry = MagicMock()
    registry.get_orchestrator_client = MagicMock(return_value=gh_client)
    return GitHubDaemonHost(identity_registry=registry)


def test_search_foreman_labeled_issues_returns_issues(monkeypatch) -> None:
    fake_repo = MagicMock()
    from datetime import UTC, datetime
    issues = [
        _FakeIssue(
            number=42,
            labels=[_FakeLabel("foreman:plan")],
            updated_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    ]
    # The host uses Github search; we stub `get_repo().get_issues()` to return
    # an iterable of issues with the label filter applied (PyGithub semantics).
    fake_repo.get_issues = MagicMock(return_value=iter(issues))

    host = _make_host_with_repo(fake_repo)
    result = host.search_foreman_labeled_issues("jeffrichley/voice")

    assert len(result) == 1
    assert result[0].number == 42
    assert result[0].labels == ["foreman:plan"]


def test_add_issue_label_calls_pygithub() -> None:
    fake_repo = MagicMock()
    fake_issue = _FakeIssue(number=42, labels=[], updated_at=None)
    fake_repo.get_issue = MagicMock(return_value=fake_issue)

    host = _make_host_with_repo(fake_repo)
    host.add_issue_label("jeffrichley/voice", 42, "foreman:failed")

    assert fake_issue.labels == [_FakeLabel(name="foreman:failed")]


def test_remove_issue_label_calls_pygithub() -> None:
    fake_repo = MagicMock()
    fake_issue = _FakeIssue(
        number=42,
        labels=[_FakeLabel("foreman:plan"), _FakeLabel("foreman:planning")],
        updated_at=None,
    )
    fake_repo.get_issue = MagicMock(return_value=fake_issue)

    host = _make_host_with_repo(fake_repo)
    host.remove_issue_label("jeffrichley/voice", 42, "foreman:planning")

    assert [lbl.name for lbl in fake_issue.labels] == ["foreman:plan"]


def test_post_issue_comment_calls_pygithub() -> None:
    fake_repo = MagicMock()
    fake_issue = _FakeIssue(number=42, labels=[], updated_at=None)
    fake_repo.get_issue = MagicMock(return_value=fake_issue)

    host = _make_host_with_repo(fake_repo)
    host.post_issue_comment("jeffrichley/voice", 42, "halted")

    assert fake_issue._last_comment == "halted"


def test_get_issue_labels_returns_label_names() -> None:
    fake_repo = MagicMock()
    fake_issue = _FakeIssue(
        number=42,
        labels=[_FakeLabel("foreman:plan"), _FakeLabel("bug")],
        updated_at=None,
    )
    fake_repo.get_issue = MagicMock(return_value=fake_issue)

    host = _make_host_with_repo(fake_repo)
    result = host.get_issue_labels("jeffrichley/voice", 42)

    assert sorted(result) == ["bug", "foreman:plan"]


def test_close_issue_calls_pygithub_edit() -> None:
    fake_repo = MagicMock()
    fake_issue = _FakeIssue(number=42, labels=[], updated_at=None)
    fake_repo.get_issue = MagicMock(return_value=fake_issue)

    host = _make_host_with_repo(fake_repo)
    host.close_issue("jeffrichley/voice", 42)

    assert fake_issue.state == "closed"


def test_find_pr_for_branch_returns_pr_number_when_found() -> None:
    fake_repo = MagicMock()
    pr_obj = MagicMock()
    pr_obj.number = 18
    pr_obj.head.ref = "foreman/issue-14"
    fake_repo.get_pulls = MagicMock(return_value=iter([pr_obj]))

    host = _make_host_with_repo(fake_repo)
    result = host.find_pr_for_branch("jeffrichley/voice", "foreman/issue-14")

    assert result == 18


def test_find_pr_for_branch_returns_none_when_absent() -> None:
    fake_repo = MagicMock()
    fake_repo.get_pulls = MagicMock(return_value=iter([]))

    host = _make_host_with_repo(fake_repo)
    result = host.find_pr_for_branch("jeffrichley/voice", "foreman/issue-99")

    assert result is None


def test_merge_pull_request_calls_pygithub_merge() -> None:
    fake_repo = MagicMock()
    fake_pr = MagicMock()
    fake_repo.get_pull = MagicMock(return_value=fake_pr)

    host = _make_host_with_repo(fake_repo)
    host.merge_pull_request("jeffrichley/voice", 18)

    fake_pr.merge.assert_called_once()


def test_get_pr_base_ref_returns_base_ref() -> None:
    fake_repo = MagicMock()
    fake_pr = _FakePR(number=25, head_branch="foreman/impl-42", base_ref="foreman/issue-42")
    fake_repo.get_pull = MagicMock(return_value=fake_pr)

    host = _make_host_with_repo(fake_repo)
    result = host.get_pr_base_ref("jeffrichley/voice", 25)

    assert result == "foreman/issue-42"


def test_is_pr_merged_for_branch_true_when_closed_merged_pr_exists() -> None:
    fake_repo = MagicMock()
    merged_pr = _FakePR(
        number=18, head_branch="foreman/issue-42", merged=True
    )
    fake_repo.get_pulls = MagicMock(return_value=iter([merged_pr]))

    host = _make_host_with_repo(fake_repo)
    result = host.is_pr_merged_for_branch("jeffrichley/voice", "foreman/issue-42")

    assert result is True
    fake_repo.get_pulls.assert_called_once_with(
        state="closed", head="jeffrichley:foreman/issue-42"
    )


def test_is_pr_merged_for_branch_false_when_no_merged_pr() -> None:
    fake_repo = MagicMock()
    fake_repo.get_pulls = MagicMock(return_value=iter([]))

    host = _make_host_with_repo(fake_repo)
    result = host.is_pr_merged_for_branch("jeffrichley/voice", "foreman/issue-99")

    assert result is False


def test_is_pr_merged_for_branch_false_when_pr_closed_but_unmerged() -> None:
    fake_repo = MagicMock()
    closed_unmerged_pr = _FakePR(
        number=18, head_branch="foreman/issue-42", merged=False
    )
    fake_repo.get_pulls = MagicMock(return_value=iter([closed_unmerged_pr]))

    host = _make_host_with_repo(fake_repo)
    result = host.is_pr_merged_for_branch("jeffrichley/voice", "foreman/issue-42")

    assert result is False


def test_retarget_pr_base_calls_pygithub_edit_with_base_arg() -> None:
    fake_repo = MagicMock()
    fake_pr = _FakePR(number=25, head_branch="foreman/impl-42", base_ref="foreman/issue-42")
    fake_repo.get_pull = MagicMock(return_value=fake_pr)

    host = _make_host_with_repo(fake_repo)
    host.retarget_pr_base("jeffrichley/voice", 25, "main")

    assert fake_pr.last_edit_kwargs == {"base": "main"}


def test_get_default_branch_returns_repo_default_branch() -> None:
    fake_repo = MagicMock()
    fake_repo.default_branch = "main"

    host = _make_host_with_repo(fake_repo)
    result = host.get_default_branch("jeffrichley/voice")

    assert result == "main"


# ----------------------------------------------------------------------
# Registry-routing invariants (issue #44)
#
# The host must ask the registry for a fresh orchestrator client on every
# API call, not cache one on the instance. That's where the refresh
# logic lives.
# ----------------------------------------------------------------------


def test_each_api_call_asks_registry_for_fresh_orchestrator_client() -> None:
    """Every host API call must route through the registry — that's where
    the 5-minute-pre-expiry refresh sits. Caching the client on the host
    would freeze the original (expiring) instance."""
    fake_repo = MagicMock()
    fake_issue = _FakeIssue(number=42, labels=[], updated_at=None)
    fake_repo.get_issue = MagicMock(return_value=fake_issue)

    gh_client = MagicMock()
    gh_client.get_repo = MagicMock(return_value=fake_repo)
    registry = MagicMock()
    registry.get_orchestrator_client = MagicMock(return_value=gh_client)
    host = GitHubDaemonHost(identity_registry=registry)

    host.add_issue_label("jeffrichley/voice", 42, "foreman:plan")
    host.close_issue("jeffrichley/voice", 42)

    # Both API calls must consult the registry; the daemon's per-call
    # lookup pattern is what makes refresh transparently propagate.
    assert registry.get_orchestrator_client.call_count >= 2


def test_host_uses_refreshed_client_after_registry_refresh() -> None:
    """When the registry rolls the orchestrator client over (the refresh
    case), the next host API call must use the new client. This is the
    behavior-level proof that token rollover propagates to the daemon's
    next API call without the host holding stale state."""
    fake_repo_old = MagicMock()
    fake_issue_old = _FakeIssue(number=42, labels=[], updated_at=None)
    fake_repo_old.get_issue = MagicMock(return_value=fake_issue_old)

    fake_repo_new = MagicMock()
    fake_issue_new = _FakeIssue(number=99, labels=[], updated_at=None)
    fake_repo_new.get_issue = MagicMock(return_value=fake_issue_new)

    old_client = MagicMock()
    old_client.get_repo = MagicMock(return_value=fake_repo_old)
    new_client = MagicMock()
    new_client.get_repo = MagicMock(return_value=fake_repo_new)

    registry = MagicMock()
    registry.get_orchestrator_client = MagicMock(side_effect=[old_client, new_client])
    host = GitHubDaemonHost(identity_registry=registry)

    host.add_issue_label("jeffrichley/voice", 42, "foreman:plan")
    host.close_issue("jeffrichley/voice", 99)

    # The second call must have used the refreshed client, not the
    # original one — otherwise the host is caching the client across
    # API calls and would silently use an expired token.
    old_client.get_repo.assert_called_once()
    new_client.get_repo.assert_called_once()
