"""Interface-contract tests for :mod:`foreman.git_host`.

We verify the abstract surface (dataclass shapes + ``GitHostProvider``
contract). Concrete-provider behavior is exercised in
``test_git_hosts_github.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foreman.git_host import BotIdentity, GitHostProvider, IssueRef, PRRef


def test_bot_identity_dataclass_shape() -> None:
    ident = BotIdentity(slug="foreman-planner", user_id=42, token="ghs_abc")
    assert ident.slug == "foreman-planner"
    assert ident.user_id == 42
    assert ident.token == "ghs_abc"


def test_issue_ref_dataclass_shape() -> None:
    ref = IssueRef(
        number=7,
        title="A title",
        body="A body",
        labels=["bug", "good first issue"],
        repo_slug="owner/repo",
    )
    assert ref.labels == ["bug", "good first issue"]


def test_pr_ref_dataclass_shape() -> None:
    ref = PRRef(
        number=99,
        url="https://github.com/o/r/pull/99",
        title="t",
        body="b",
        branch="feat/x",
        base_branch="main",
        repo_slug="o/r",
    )
    assert ref.number == 99
    assert ref.base_branch == "main"


def test_git_host_provider_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        GitHostProvider()  # type: ignore[abstract]


class _FakeProvider(GitHostProvider):
    """Verifies the abstract surface — every abstract method must be
    overridden in a concrete subclass."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def get_issue(self, repo_slug: str, issue_number: int) -> IssueRef:
        self.calls.append(("get_issue", (repo_slug, issue_number)))
        return IssueRef(number=issue_number, title="", body="", labels=[], repo_slug=repo_slug)

    def get_default_branch(self, repo_slug: str) -> str:
        self.calls.append(("get_default_branch", (repo_slug,)))
        return "main"

    def commit_files_to_worktree(
        self, worktree_path: Path, files: dict[str, str], message: str
    ) -> str:
        self.calls.append(("commit_files_to_worktree", (worktree_path, files, message)))
        return "deadbeef"

    def push_branch(self, worktree_path: Path, branch: str) -> None:
        self.calls.append(("push_branch", (worktree_path, branch)))

    def open_pull_request(
        self, repo_slug: str, title: str, body: str, base: str, head: str
    ) -> PRRef:
        self.calls.append(("open_pull_request", (repo_slug, title, body, base, head)))
        return PRRef(
            number=1,
            url="https://example/pull/1",
            title=title,
            body=body,
            branch=head,
            base_branch=base,
            repo_slug=repo_slug,
        )

    def update_issue_labels(
        self,
        repo_slug: str,
        issue_number: int,
        add: list[str],
        remove: list[str],
    ) -> None:
        self.calls.append(("update_issue_labels", (repo_slug, issue_number, add, remove)))

    def post_issue_comment(
        self, repo_slug: str, issue_number: int, body: str
    ) -> None:
        self.calls.append(("post_issue_comment", (repo_slug, issue_number, body)))


def test_fake_provider_is_instantiable_and_callable() -> None:
    fake = _FakeProvider()
    ref = fake.get_issue("o/r", 3)
    assert ref.number == 3
    sha = fake.commit_files_to_worktree(Path("/tmp/x"), {"f.md": "x"}, "msg")
    assert sha == "deadbeef"
