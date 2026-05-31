"""Tests for :class:`foreman.git_hosts.github.GitHubProvider`.

Strategy: real ``git`` subprocess for the worktree-side operations (so we
catch real git error shapes), mocked PyGithub for the API-side operations.
The ``push_branch`` test stubs ``subprocess.run`` so we can assert on the
constructed token URL without needing a real remote.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from foreman.git_host import BotIdentity
from foreman.git_hosts.github import GitHubProvider, _extract_repo_slug


def _identity(token: str = "ghs_test_token") -> BotIdentity:
    return BotIdentity(slug="foreman-planner", user_id=12345, token=token)


# ----------------------------------------------------------------------
# URL parsing helpers
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/owner/name.git", "owner/name"),
        ("https://github.com/owner/name", "owner/name"),
        ("git@github.com:owner/name.git", "owner/name"),
        (
            "https://x-access-token:ghs_xxx@github.com/owner/name.git",
            "owner/name",
        ),
    ],
)
def test_extract_repo_slug_handles_url_forms(url: str, expected: str) -> None:
    assert _extract_repo_slug(url) == expected


def test_extract_repo_slug_rejects_unparseable() -> None:
    with pytest.raises(ValueError, match="Cannot parse"):
        _extract_repo_slug("https://example.com/foo/bar.git")


# ----------------------------------------------------------------------
# API-side operations (PyGithub mocked)
# ----------------------------------------------------------------------


def test_get_issue_returns_issue_ref_from_pygithub() -> None:
    fake_issue = MagicMock()
    fake_issue.number = 42
    fake_issue.title = "T"
    fake_issue.body = "B"
    fake_issue.labels = [MagicMock(name="x"), MagicMock(name="y")]
    fake_issue.labels[0].name = "x"
    fake_issue.labels[1].name = "y"
    fake_repo = MagicMock()
    fake_repo.get_issue.return_value = fake_issue
    fake_client = MagicMock()
    fake_client.get_repo.return_value = fake_repo

    provider = GitHubProvider(identity=_identity(), client=fake_client)
    ref = provider.get_issue("o/r", 42)

    assert ref.number == 42
    assert ref.title == "T"
    assert ref.body == "B"
    assert ref.labels == ["x", "y"]
    assert ref.repo_slug == "o/r"
    fake_client.get_repo.assert_called_once_with("o/r")


def test_get_issue_tolerates_none_body_and_title() -> None:
    fake_issue = MagicMock()
    fake_issue.number = 1
    fake_issue.title = None
    fake_issue.body = None
    fake_issue.labels = []
    fake_repo = MagicMock()
    fake_repo.get_issue.return_value = fake_issue
    fake_client = MagicMock()
    fake_client.get_repo.return_value = fake_repo

    provider = GitHubProvider(identity=_identity(), client=fake_client)
    ref = provider.get_issue("o/r", 1)
    assert ref.body == ""
    assert ref.title == ""


def test_get_default_branch_returns_repo_default() -> None:
    fake_repo = MagicMock()
    fake_repo.default_branch = "trunk"
    fake_client = MagicMock()
    fake_client.get_repo.return_value = fake_repo

    provider = GitHubProvider(identity=_identity(), client=fake_client)
    assert provider.get_default_branch("o/r") == "trunk"


def test_open_pull_request_returns_pr_ref_from_pygithub() -> None:
    fake_pr = MagicMock()
    fake_pr.number = 99
    fake_pr.html_url = "https://github.com/o/r/pull/99"
    fake_pr.title = "spec: x"
    fake_pr.body = "body"
    fake_repo = MagicMock()
    fake_repo.create_pull.return_value = fake_pr
    fake_client = MagicMock()
    fake_client.get_repo.return_value = fake_repo

    provider = GitHubProvider(identity=_identity(), client=fake_client)
    ref = provider.open_pull_request(
        repo_slug="o/r",
        title="spec: x",
        body="body",
        base="main",
        head="foreman/issue-7",
    )

    assert ref.number == 99
    assert ref.url == "https://github.com/o/r/pull/99"
    assert ref.branch == "foreman/issue-7"
    assert ref.base_branch == "main"
    fake_repo.create_pull.assert_called_once_with(
        title="spec: x", body="body", base="main", head="foreman/issue-7"
    )


def test_update_issue_labels_removes_then_adds() -> None:
    fake_issue = MagicMock()
    fake_repo = MagicMock()
    fake_repo.get_issue.return_value = fake_issue
    fake_client = MagicMock()
    fake_client.get_repo.return_value = fake_repo

    provider = GitHubProvider(identity=_identity(), client=fake_client)
    provider.update_issue_labels(
        repo_slug="o/r",
        issue_number=7,
        add=["foreman:spec-review"],
        remove=["foreman:plan"],
    )

    fake_issue.remove_from_labels.assert_called_once_with("foreman:plan")
    fake_issue.add_to_labels.assert_called_once_with("foreman:spec-review")


# ----------------------------------------------------------------------
# Worktree-side operations (real git in tmp_path)
# ----------------------------------------------------------------------


def _init_worktree(tmp_path: Path) -> Path:
    """Init a bare-bones git repo + checkout for use as a worktree stand-in."""
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    # Seed identity so the seed commit succeeds even before configure_worktree_identity.
    subprocess.run(
        ["git", "config", "user.email", "seed@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Seed"], cwd=repo, check=True, capture_output=True
    )
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True
    )
    return repo


def test_configure_worktree_identity_sets_bot_attribution(tmp_path: Path) -> None:
    wt = _init_worktree(tmp_path)
    provider = GitHubProvider(identity=_identity(), client=MagicMock())
    provider.configure_worktree_identity(wt)

    name = subprocess.run(
        ["git", "config", "user.name"], cwd=wt, check=True, capture_output=True, text=True
    ).stdout.strip()
    email = subprocess.run(
        ["git", "config", "user.email"], cwd=wt, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert name == "foreman-planner[bot]"
    assert email == "12345+foreman-planner[bot]@users.noreply.github.com"


def test_commit_files_to_worktree_writes_adds_commits_and_returns_sha(
    tmp_path: Path,
) -> None:
    wt = _init_worktree(tmp_path)
    provider = GitHubProvider(identity=_identity(), client=MagicMock())
    provider.configure_worktree_identity(wt)

    sha = provider.commit_files_to_worktree(
        worktree_path=wt,
        files={
            "docs/specs/x.md": "# Spec\n",
            "notes/inner/y.txt": "y\n",
        },
        message="spec: add x",
    )

    assert sha and len(sha) == 40
    assert (wt / "docs/specs/x.md").read_text() == "# Spec\n"
    assert (wt / "notes/inner/y.txt").read_text() == "y\n"
    # Latest commit message matches
    msg = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert msg == "spec: add x"
    # Author == bot identity
    author = subprocess.run(
        ["git", "log", "-1", "--pretty=%an <%ae>"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert author == "foreman-planner[bot] <12345+foreman-planner[bot]@users.noreply.github.com>"


def test_push_branch_uses_installation_token_url(tmp_path: Path) -> None:
    """Push must use an HTTPS URL embedding ``x-access-token:<token>``."""
    wt = _init_worktree(tmp_path)
    # Wire a fake remote so the `git config --get remote.origin.url` call has
    # something to return.
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/name.git"],
        cwd=wt,
        check=True,
        capture_output=True,
    )

    real_run = subprocess.run

    push_calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(cmd, list) and len(cmd) > 1 and cmd[0] == "git" and cmd[1] == "push":
            push_calls.append(cmd)
            completed: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
                cmd, 0, stdout="", stderr=""
            )
            return completed
        return real_run(cmd, *args, **kwargs)

    provider = GitHubProvider(identity=_identity("ghs_abc"), client=MagicMock())

    with patch("foreman.git_hosts.github.subprocess.run", side_effect=fake_run):
        provider.push_branch(worktree_path=wt, branch="foreman/issue-7")

    assert len(push_calls) == 1
    pushed = push_calls[0]
    assert pushed[0:2] == ["git", "push"]
    url = pushed[2]
    assert url == "https://x-access-token:ghs_abc@github.com/owner/name.git"
    # refspec is branch:branch (so server-side rejects accidental force-push semantics)
    assert pushed[3] == "foreman/issue-7:foreman/issue-7"
