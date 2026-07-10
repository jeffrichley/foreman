"""Tests for :class:`foreman.git_hosts.github.GitHubProvider`.

Strategy: real ``git`` subprocess for the worktree-side operations (so we
catch real git error shapes), mocked PyGithub for the API-side operations.
The ``push_branch`` test stubs ``subprocess.run`` so we can assert on the
constructed token URL without needing a real remote.
"""

from __future__ import annotations

import re
import subprocess
import traceback
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from foreman.git_host import BotIdentity
from foreman.git_hosts._errors import GitCommandError, sanitize_cmd
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


def test_get_issue_comments_returns_chronological_comment_refs() -> None:
    """foreman#328: walk PyGithub's IssueComment iterable into CommentRef
    items in chronological order (oldest first). The provider sorts
    defensively even when the upstream returns chronological by default.
    """
    from datetime import datetime

    # Two PyGithub-shape mock comments — intentionally out-of-order to
    # prove the provider's defense-in-depth sort.
    newer = MagicMock()
    newer.user.login = "bob"
    newer.created_at = datetime(2026, 6, 17, 22, 0, tzinfo=UTC)
    newer.body = "second"

    older = MagicMock()
    older.user.login = "alice"
    older.created_at = datetime(2026, 6, 17, 20, 0, tzinfo=UTC)
    older.body = "first"

    fake_issue = MagicMock()
    fake_issue.get_comments.return_value = [newer, older]
    fake_repo = MagicMock()
    fake_repo.get_issue.return_value = fake_issue
    fake_client = MagicMock()
    fake_client.get_repo.return_value = fake_repo

    provider = GitHubProvider(identity=_identity(), client=fake_client)
    refs = provider.get_issue_comments("o/r", 42)

    assert [r.author_login for r in refs] == ["alice", "bob"]
    assert [r.body for r in refs] == ["first", "second"]
    fake_repo.get_issue.assert_called_once_with(42)


def test_get_issue_comments_tolerates_none_body() -> None:
    """foreman#328: PyGithub may yield ``IssueComment.body is None``
    on an empty comment; the provider coerces to empty string for the
    role-side ``CommentRef``."""
    from datetime import datetime

    fake_comment = MagicMock()
    fake_comment.user.login = "alice"
    fake_comment.created_at = datetime(2026, 6, 17, 20, 0, tzinfo=UTC)
    fake_comment.body = None

    fake_issue = MagicMock()
    fake_issue.get_comments.return_value = [fake_comment]
    fake_repo = MagicMock()
    fake_repo.get_issue.return_value = fake_issue
    fake_client = MagicMock()
    fake_client.get_repo.return_value = fake_repo

    provider = GitHubProvider(identity=_identity(), client=fake_client)
    refs = provider.get_issue_comments("o/r", 42)
    assert refs[0].body == ""


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
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    # Seed identity so the seed commit succeeds. Bot identity for commit_files_to_worktree
    # is injected via env vars (foreman#53); this seed config is the "human-pre-set" state
    # that the regression test below asserts is left untouched after a bot commit.
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
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True)
    return repo


def test_commit_files_to_worktree_does_not_mutate_repo_local_config(
    tmp_path: Path,
) -> None:
    """foreman#53 regression guard: after a bot commit, the worktree's
    ``.git/config`` user.* fields are untouched. Worktrees share
    ``.git/config`` with the parent repo, so any persistent mutation
    here leaks the bot identity into subsequent human commits in the
    same checkout. Identity must flow through env vars on the commit
    subprocess, not through ``git config``.
    """
    wt = _init_worktree(tmp_path)
    provider = GitHubProvider(identity=_identity(), client=MagicMock())

    provider.commit_files_to_worktree(
        worktree_path=wt,
        files={"docs/specs/x.md": "# Spec\n"},
        message="spec: add x",
    )

    name = subprocess.run(
        ["git", "config", "user.name"], cwd=wt, check=True, capture_output=True, text=True
    ).stdout.strip()
    email = subprocess.run(
        ["git", "config", "user.email"], cwd=wt, check=True, capture_output=True, text=True
    ).stdout.strip()
    # Seed values from _init_worktree must survive untouched.
    assert name == "Seed"
    assert email == "seed@example.com"


def test_commit_files_to_worktree_writes_adds_commits_and_returns_sha(
    tmp_path: Path,
) -> None:
    wt = _init_worktree(tmp_path)
    provider = GitHubProvider(identity=_identity(), client=MagicMock())

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


def test_commit_files_to_worktree_appends_provenance_trailers(tmp_path: Path) -> None:
    """Issue #347: provenance_trailers (when non-empty) are spliced as
    one ``--trailer "<value>"`` flag per entry into ``git commit``.

    Verifies BOTH a ``Supervised-by:`` and a ``Signed-off-by:`` trailer
    land in the commit body, in the order the caller specified.
    """
    wt = _init_worktree(tmp_path)
    provider = GitHubProvider(identity=_identity(), client=MagicMock())

    trailers = [
        "Supervised-by: Wren Richley <wren@example.com>",
        "Signed-off-by: Jeff Richley <jeff@example.com>",
    ]
    provider.commit_files_to_worktree(
        worktree_path=wt,
        files={"docs/specs/x.md": "# Spec\n"},
        message="docs(spec): add x",
        provenance_trailers=trailers,
    )

    body = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Supervised-by: Wren Richley <wren@example.com>" in body
    assert "Signed-off-by: Jeff Richley <jeff@example.com>" in body


def test_commit_files_to_worktree_planner_trailer_pattern_end_to_end(
    tmp_path: Path,
) -> None:
    """Issue #347: end-to-end test for the Planner's
    ``resolve_operator → provenance_trailers list → commit body`` flow.

    Builds a real OperatorConfig, calls the resolver, formats the two
    trailers the same way :func:`foreman.roles.planner._run_planner_core`
    does at the host call site, and asserts the resulting commit body
    carries BOTH trailers in the expected shape. This pins the contract
    on the integration boundary between the resolver (pure function in
    ``foreman.v4.config``) and the host commit invocation
    (``foreman.git_hosts.github.GitHubProvider.commit_files_to_worktree``).
    """
    from foreman.v4.config import (
        AppCredentials,
        AppsConfig,
        OperatorConfig,
        OperatorIdentity,
        OrchestratorConfig,
        ProjectConfig,
        StorageConfig,
        V4Config,
        resolve_operator,
    )

    wt = _init_worktree(tmp_path)
    provider = GitHubProvider(identity=_identity(), client=MagicMock())

    config = V4Config(
        storage=StorageConfig(engine="postgres", dsn="postgresql://test/test"),
        log_dir="/tmp/logs",
        apps=AppsConfig(
            planner=AppCredentials(app_id=1, private_key_path="/dev/null"),
            reviewer=AppCredentials(app_id=2, private_key_path="/dev/null"),
            fixer=AppCredentials(app_id=3, private_key_path="/dev/null"),
            worker=AppCredentials(app_id=4, private_key_path="/dev/null"),
        ),
        orchestrator=OrchestratorConfig(app_id=5, private_key_path="/dev/null"),
        operator=OperatorConfig(
            supervisor=OperatorIdentity(name="Wren Richley", email="wren@example.com"),
            signer=OperatorIdentity(name="Jeff Richley", email="jeff@example.com"),
        ),
        projects=[
            ProjectConfig(
                name="p",
                repo="o/r",
                local_clone_path="/tmp/r",
            ),
        ],
    )
    op = resolve_operator(config.projects[0], config)
    trailers = [
        f"Supervised-by: {op.supervisor.name} <{op.supervisor.email}>",
        f"Signed-off-by: {op.signer.name} <{op.signer.email}>",
    ]
    provider.commit_files_to_worktree(
        worktree_path=wt,
        files={"docs/superpowers/specs/foreman-issue-347-spec.md": "spec\n"},
        message="docs(spec): add foo per spec",
        provenance_trailers=trailers,
    )

    body = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Supervised-by: Wren Richley <wren@example.com>" in body
    assert "Signed-off-by: Jeff Richley <jeff@example.com>" in body


def test_commit_files_to_worktree_omits_trailers_when_none(tmp_path: Path) -> None:
    """Issue #347: backwards-compatible default. ``None`` / empty list
    leaves the commit body untouched (no spurious trailers)."""
    wt = _init_worktree(tmp_path)
    provider = GitHubProvider(identity=_identity(), client=MagicMock())

    provider.commit_files_to_worktree(
        worktree_path=wt,
        files={"docs/specs/x.md": "# Spec\n"},
        message="docs(spec): add x",
        provenance_trailers=None,
    )

    body = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Supervised-by:" not in body
    assert "Signed-off-by:" not in body


def test_commit_files_to_worktree_is_idempotent_when_content_matches_head(
    tmp_path: Path,
) -> None:
    """foreman#117: when a prior killed-mid-flight Planner run already
    committed the spec doc to the worktree, the next call must not
    crash on `git commit` returning "nothing to commit, working tree
    clean". Treat empty staged-diff as success and return the existing
    HEAD SHA so the caller pushes the prior commit.
    """
    wt = _init_worktree(tmp_path)
    provider = GitHubProvider(identity=_identity(), client=MagicMock())

    spec_relpath = "docs/specs/x.md"
    spec_content = "# Spec\n\nthe planner output for issue 117\n"
    commit_message = "docs(spec): add x"

    first_sha = provider.commit_files_to_worktree(
        worktree_path=wt,
        files={spec_relpath: spec_content},
        message=commit_message,
    )

    second_sha = provider.commit_files_to_worktree(
        worktree_path=wt,
        files={spec_relpath: spec_content},
        message=commit_message,
    )

    assert second_sha == first_sha
    commit_count = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # 1 seed + 1 spec commit; the second call must NOT add a third.
    assert commit_count == "2"


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
    assert pushed[2] == "--force-with-lease"
    url = pushed[3]
    assert url == "https://x-access-token:ghs_abc@github.com/owner/name.git"
    # refspec is branch:branch; --force-with-lease guards against unexpected
    # remote movement without silently overwriting concurrent writes.
    assert pushed[4] == "foreman/issue-7:foreman/issue-7"


# ----------------------------------------------------------------------
# Defect B — git stderr surfaces in the raised exception
# ----------------------------------------------------------------------


def _make_called_process_error(cmd: list[str], stderr: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(returncode=1, cmd=cmd, output="", stderr=stderr)


def test_git_failure_surfaces_stderr_message_on_push_branch(tmp_path: Path) -> None:
    """Defect B: git's stderr (the real diagnostic) must appear in str(exc)."""
    wt = _init_worktree(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/name.git"],
        cwd=wt,
        check=True,
        capture_output=True,
    )

    real_run = subprocess.run
    stderr_msg = (
        "refusing to allow a GitHub App to create or update workflow "
        ".github/workflows/release.yml without workflows permission"
    )

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(cmd, list) and len(cmd) > 1 and cmd[0] == "git" and cmd[1] == "push":
            raise _make_called_process_error(cmd, stderr_msg)
        return real_run(cmd, *args, **kwargs)

    provider = GitHubProvider(identity=_identity("ghs_abc"), client=MagicMock())

    with patch("foreman.git_hosts.github.subprocess.run", side_effect=fake_run):
        with pytest.raises(GitCommandError) as excinfo:
            provider.push_branch(worktree_path=wt, branch="foreman/issue-7")

    exc = excinfo.value
    assert "workflows permission" in str(exc)
    assert "workflows permission" in exc.stderr
    assert exc.returncode == 1


def test_git_failure_preserves_stderr_attribute_on_non_push_op(
    tmp_path: Path,
) -> None:
    """Failures in non-push git ops must also raise GitCommandError with stderr."""
    wt = _init_worktree(tmp_path)
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Trigger on `git add` (early in commit_files_to_worktree) so the
        # error surface is exercised before any commit env-var muddies the test.
        if isinstance(cmd, list) and len(cmd) > 1 and cmd[0] == "git" and cmd[1] == "add":
            raise _make_called_process_error(cmd, "fatal: not in a git repo")
        return real_run(cmd, *args, **kwargs)

    provider = GitHubProvider(identity=_identity(), client=MagicMock())
    with patch("foreman.git_hosts.github.subprocess.run", side_effect=fake_run):
        with pytest.raises(GitCommandError) as excinfo:
            provider.commit_files_to_worktree(
                worktree_path=wt,
                files={"x.md": "x\n"},
                message="x",
            )

    assert "not in a git repo" in excinfo.value.stderr
    assert excinfo.value.returncode == 1


# ----------------------------------------------------------------------
# Defect C — install token never leaks into any error surface
# ----------------------------------------------------------------------


LIVE_TOKEN = "ghs_FAKETOKEN1234567890abcdef"


def test_push_branch_failure_redacts_token_from_every_surface(tmp_path: Path) -> None:
    """Defect C: a known token must not appear in str/repr/attrs/traceback."""
    wt = _init_worktree(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/name.git"],
        cwd=wt,
        check=True,
        capture_output=True,
    )

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(cmd, list) and len(cmd) > 1 and cmd[0] == "git" and cmd[1] == "push":
            # stderr also references the URL — git often echoes the remote on
            # failure. Make sure redaction covers stderr too.
            raise _make_called_process_error(
                cmd,
                stderr=(
                    f"remote: error pushing to "
                    f"https://x-access-token:{LIVE_TOKEN}@github.com/owner/name.git"
                ),
            )
        return real_run(cmd, *args, **kwargs)

    provider = GitHubProvider(identity=_identity(LIVE_TOKEN), client=MagicMock())

    with patch("foreman.git_hosts.github.subprocess.run", side_effect=fake_run):
        with pytest.raises(GitCommandError) as excinfo:
            provider.push_branch(worktree_path=wt, branch="foreman/issue-7")

    exc = excinfo.value
    token_re = re.compile(re.escape(LIVE_TOKEN))

    # Direct surfaces
    assert not token_re.search(str(exc)), "token leaked into str(exc)"
    assert not token_re.search(repr(exc)), "token leaked into repr(exc)"
    assert not token_re.search(exc.stderr), "token leaked into exc.stderr"
    for arg in exc.cmd:
        assert not token_re.search(arg), f"token leaked into exc.cmd arg: {arg!r}"

    # All other public attributes (anything string-valued)
    for attr_name in dir(exc):
        if attr_name.startswith("_"):
            continue
        try:
            value = getattr(exc, attr_name)
        except Exception:
            continue
        if isinstance(value, str):
            assert not token_re.search(value), f"token leaked into exc.{attr_name}"
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str):
                    assert not token_re.search(item), f"token leaked into exc.{attr_name}"

    # Traceback formatting (this is the surface that hit the chat transcript)
    formatted_tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert not token_re.search(formatted_tb), "token leaked into formatted traceback"


@pytest.mark.parametrize(
    "token",
    [
        "ghs_installationTOKEN0000000000000000",
        "ghp_classicPAT00000000000000000000000",
        "gho_oauthToken000000000000000000000000",
        "github_pat_finegrained_AAAAA_BBBBBBBBBBBBBBBB",
    ],
)
def test_sanitize_cmd_redacts_all_github_token_prefixes(token: str) -> None:
    """Defect C: every documented GitHub token prefix is redacted."""
    cmd = ["git", "push", f"https://x-access-token:{token}@github.com/o/r.git", "br:br"]
    sanitized = sanitize_cmd(cmd)
    joined = " ".join(sanitized)
    assert token not in joined
    assert "[REDACTED]" in joined


def test_sanitize_cmd_preserves_non_token_args() -> None:
    """Defect C: legitimate args must pass through unchanged."""
    cmd = [
        "git",
        "push",
        "https://github.com/owner/name.git",
        "foreman/issue-10:foreman/issue-10",
    ]
    assert sanitize_cmd(cmd) == cmd
    # And a branch name that contains underscores + alphanum but no prefix.
    assert sanitize_cmd(["foreman_planner_branch_42"]) == ["foreman_planner_branch_42"]


# ----------------------------------------------------------------------
# Defect (issue #10) — env-var leak into target-repo hooks
#
# When Foreman runs git push, the target repo's pre-push hook inherits
# the parent process's environment. If VIRTUAL_ENV (or other Python/uv
# vars) point at Foreman's venv, ``uv run --no-sync`` in the hook will
# detect the mismatch and create a fresh empty .venv in the target
# worktree — then mypy/pytest fail because the venv has no deps.
#
# Fix: _git filters these vars out of the subprocess env before running.
# ----------------------------------------------------------------------


def _capture_push_run(
    wt: Path, *, env_capture: dict[str, dict[str, str] | None]
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a fake subprocess.run that records ``env=`` from the push call."""
    real_run = subprocess.run

    def fake_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(cmd, list) and len(cmd) > 1 and cmd[0] == "git" and cmd[1] == "push":
            env_capture["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    return fake_run


def _setup_push_worktree(tmp_path: Path) -> Path:
    """Init a worktree with a remote configured, ready for push_branch."""
    wt = _init_worktree(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/name.git"],
        cwd=wt,
        check=True,
        capture_output=True,
    )
    return wt


def test_git_subprocess_drops_virtual_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #10: VIRTUAL_ENV must not leak into the git subprocess env."""
    monkeypatch.setenv("VIRTUAL_ENV", "sentinel-do-not-leak")
    wt = _setup_push_worktree(tmp_path)
    capture: dict[str, dict[str, str] | None] = {"env": None}

    provider = GitHubProvider(identity=_identity("ghs_abc"), client=MagicMock())
    with patch(
        "foreman.git_hosts.github.subprocess.run",
        side_effect=_capture_push_run(wt, env_capture=capture),
    ):
        provider.push_branch(worktree_path=wt, branch="foreman/issue-7")

    env = capture["env"]
    assert env is not None, "subprocess.run for git push must receive an explicit env="
    assert "VIRTUAL_ENV" not in env, (
        "VIRTUAL_ENV leaked into git subprocess — pre-push hooks will mis-detect "
        "Foreman's venv and create a stray .venv in the target worktree"
    )


def test_git_subprocess_drops_python_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #10: PYTHONPATH must not leak (would pull foreman modules into hook)."""
    monkeypatch.setenv("PYTHONPATH", "E:/workspaces/ai/agents/foreman/src")
    wt = _setup_push_worktree(tmp_path)
    capture: dict[str, dict[str, str] | None] = {"env": None}

    provider = GitHubProvider(identity=_identity("ghs_abc"), client=MagicMock())
    with patch(
        "foreman.git_hosts.github.subprocess.run",
        side_effect=_capture_push_run(wt, env_capture=capture),
    ):
        provider.push_branch(worktree_path=wt, branch="foreman/issue-7")

    env = capture["env"]
    assert env is not None
    assert "PYTHONPATH" not in env


def test_git_subprocess_preserves_path_and_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #10: harmless host vars must still be inherited (hooks need them)."""
    # Set up the worktree FIRST (uses real git on the host PATH) so the
    # monkeypatched sentinel values don't break worktree initialization.
    wt = _setup_push_worktree(tmp_path)

    # Sentinel values for vars the filter MUST preserve. Use values that
    # are uniquely identifiable in the captured env dict so we don't get
    # false positives from the real host values.
    monkeypatch.setenv("HOME", "/tmp/foreman-test-home-sentinel")
    monkeypatch.setenv("USERPROFILE", "C:/Users/foreman-test-sentinel")
    monkeypatch.setenv("GH_TOKEN", "ghp_legacy_token_sentinel")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    # PATH is left at the real host value — overwriting it would break
    # any nested real-git calls during the push setup. We assert it
    # passes through (non-empty) rather than equals a sentinel.

    capture: dict[str, dict[str, str] | None] = {"env": None}

    provider = GitHubProvider(identity=_identity("ghs_abc"), client=MagicMock())
    with patch(
        "foreman.git_hosts.github.subprocess.run",
        side_effect=_capture_push_run(wt, env_capture=capture),
    ):
        provider.push_branch(worktree_path=wt, branch="foreman/issue-7")

    env = capture["env"]
    assert env is not None
    # Things hooks legitimately depend on must still be there.
    assert env.get("PATH"), "PATH must be preserved — hooks need it to find executables"
    assert env.get("HOME") == "/tmp/foreman-test-home-sentinel"
    assert env.get("USERPROFILE") == "C:/Users/foreman-test-sentinel"
    assert env.get("GH_TOKEN") == "ghp_legacy_token_sentinel"
    assert env.get("LANG") == "en_US.UTF-8"


@pytest.mark.parametrize(
    "var",
    [
        "VIRTUAL_ENV",
        "PYTHONHOME",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
        "UV_PROJECT",
        "UV_PYTHON",
        "UV_WORKING_DIR",
        "UV_SYSTEM_PYTHON",
        "UV_PYTHON_PREFERENCE",
        "UV_PYTHON_SEARCH_PATH",
        "UV_MANAGED_PYTHON",
        "UV_NO_MANAGED_PYTHON",
        "CONDA_PREFIX",
    ],
)
def test_git_subprocess_drops_all_blocked_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    """Issue #10: every Python/uv env var that could mis-direct a foreign
    worktree's hooks must be dropped.

    The blocklist covers anything that could:
    - point a Python tool at the wrong interpreter (VIRTUAL_ENV, PYTHONHOME,
      CONDA_PREFIX, UV_PYTHON, UV_SYSTEM_PYTHON, UV_MANAGED_PYTHON,
      UV_NO_MANAGED_PYTHON, UV_PYTHON_PREFERENCE, UV_PYTHON_SEARCH_PATH)
    - point uv at the wrong project/env directory (UV_PROJECT,
      UV_PROJECT_ENVIRONMENT, UV_WORKING_DIR)
    - leak foreman's importable modules into the hook (PYTHONPATH)

    UV_CACHE_DIR is intentionally NOT blocked — cache sharing across
    repos is safe and desirable.
    """
    monkeypatch.setenv(var, "sentinel-do-not-leak")
    wt = _setup_push_worktree(tmp_path)
    capture: dict[str, dict[str, str] | None] = {"env": None}

    provider = GitHubProvider(identity=_identity("ghs_abc"), client=MagicMock())
    with patch(
        "foreman.git_hosts.github.subprocess.run",
        side_effect=_capture_push_run(wt, env_capture=capture),
    ):
        provider.push_branch(worktree_path=wt, branch="foreman/issue-7")

    env = capture["env"]
    assert env is not None, f"subprocess.run for git push must receive env= (var={var})"
    assert var not in env, f"{var}=sentinel-do-not-leak leaked into git subprocess env"


def test_push_branch_uses_force_with_lease(tmp_path: Path) -> None:
    """Regression for foreman#494: push_branch must use --force-with-lease.

    After the Fixer rebases its branch (rewriting commit SHAs), a plain
    ``git push`` is rejected non-fast-forward. ``--force-with-lease`` allows
    the rewritten history to land while still refusing to overwrite if the
    remote moved unexpectedly — the safe form for single-owner bot branches.
    """
    wt = _init_worktree(tmp_path)
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
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    provider = GitHubProvider(identity=_identity("ghs_abc"), client=MagicMock())

    with patch("foreman.git_hosts.github.subprocess.run", side_effect=fake_run):
        provider.push_branch(worktree_path=wt, branch="foreman/impl-494")

    assert len(push_calls) == 1
    assert "--force-with-lease" in push_calls[0], (
        "push_branch must use --force-with-lease so Fixer rebases survive "
        "(foreman#494: plain push was rejected non-fast-forward after rebase)"
    )


# ----------------------------------------------------------------------
# foreman#484 — push_branch retry on non-fast-forward rejection
#
# When a foreman/issue-N or foreman/impl-N remote branch has divergent
# history from a prior run, a plain push is rejected with
# "! [rejected] ... (fetch first)" or "(non-fast-forward)". Foreman
# owns these branch namespaces, so a force-refresh is safe. The fix
# adds a try/except around the initial push: on rejection it retries
# with --force-with-lease; all other errors re-raise immediately.
# ----------------------------------------------------------------------


def test_push_branch_retries_with_force_on_non_fast_forward(tmp_path: Path) -> None:
    """push_branch() retries with --force-with-lease when rejected as non-fast-forward."""
    wt = _init_worktree(tmp_path)
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
            push_calls.append(list(cmd))
            if len(push_calls) == 1:
                raise subprocess.CalledProcessError(
                    1,
                    cmd,
                    output="",
                    stderr="! [rejected] foreman/issue-7 -> foreman/issue-7 (fetch first)\n",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    provider = GitHubProvider(identity=_identity("ghs_abc"), client=MagicMock())

    with patch("foreman.git_hosts.github.subprocess.run", side_effect=fake_run):
        provider.push_branch(worktree_path=wt, branch="foreman/issue-7")

    assert len(push_calls) == 2, "expected initial push + force-with-lease retry"
    assert "--force-with-lease" in push_calls[1], "retry must use --force-with-lease"


def test_push_branch_does_not_retry_on_non_rejection_error(tmp_path: Path) -> None:
    """A push failure that is NOT a non-fast-forward rejection must not trigger retry."""
    wt = _init_worktree(tmp_path)
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
            push_calls.append(list(cmd))
            raise subprocess.CalledProcessError(
                1,
                cmd,
                output="",
                stderr="refusing to allow a GitHub App to create or update workflow without workflows permission",
            )
        return real_run(cmd, *args, **kwargs)

    provider = GitHubProvider(identity=_identity("ghs_abc"), client=MagicMock())

    with patch("foreman.git_hosts.github.subprocess.run", side_effect=fake_run):
        with pytest.raises(GitCommandError) as excinfo:
            provider.push_branch(worktree_path=wt, branch="foreman/issue-7")

    assert len(push_calls) == 1, "only one push attempt, no retry"
    assert "workflows permission" in excinfo.value.stderr


def test_git_subprocess_preserves_uv_cache_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #10: UV_CACHE_DIR is safe to share — must NOT be blocked."""
    monkeypatch.setenv("UV_CACHE_DIR", "C:/Users/test/.cache/uv")
    wt = _setup_push_worktree(tmp_path)
    capture: dict[str, dict[str, str] | None] = {"env": None}

    provider = GitHubProvider(identity=_identity("ghs_abc"), client=MagicMock())
    with patch(
        "foreman.git_hosts.github.subprocess.run",
        side_effect=_capture_push_run(wt, env_capture=capture),
    ):
        provider.push_branch(worktree_path=wt, branch="foreman/issue-7")

    env = capture["env"]
    assert env is not None
    assert env.get("UV_CACHE_DIR") == "C:/Users/test/.cache/uv"
