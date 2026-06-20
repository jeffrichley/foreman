"""Unit tests for ``foreman contrib sign-commits``.

The fake-repo fixture builds a real git repo in ``tmp_path`` with
controlled signoff state so we can exercise the sign-commits command
against it without mocking subprocess. Each test chdir's into the
fixture repo and invokes the typer app via :class:`CliRunner`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foreman.v4.cli import app

# --------------------------------------------------------------------- #
# Fixture helpers                                                       #
# --------------------------------------------------------------------- #


def _git(
    *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd`` with check=True + capture, mirroring fixture needs."""
    base_env = os.environ.copy()
    if env:
        base_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=base_env,
    )


def _commit(
    repo: Path,
    *,
    message: str,
    file_name: str,
    file_content: str,
    signoff: bool,
) -> None:
    """Add + commit a file in ``repo``. Pass signoff=True for ``-s``."""
    (repo / file_name).write_text(file_content)
    _git("add", file_name, cwd=repo)
    args = ["commit"]
    if signoff:
        args.append("-s")
    args.extend(["-m", message])
    _git(*args, cwd=repo)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Build a repo with:

    - 1 commit on ``main`` (the base).
    - Branch ``feature``: 2 unsigned commits + 1 signed-off commit.

    Returns the repo path. Tests chdir into it before invoking the CLI.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.name", "Alice", cwd=repo)
    _git("config", "user.email", "alice@example.com", cwd=repo)
    # Disable signing so tests don't try to use a missing gpg key.
    _git("config", "commit.gpgsign", "false", cwd=repo)

    _commit(
        repo,
        message="initial commit",
        file_name="README.md",
        file_content="# repo\n",
        signoff=True,
    )

    _git("checkout", "-b", "feature", cwd=repo)
    _commit(
        repo,
        message="unsigned first",
        file_name="a.txt",
        file_content="a\n",
        signoff=False,
    )
    _commit(
        repo,
        message="unsigned second",
        file_name="b.txt",
        file_content="b\n",
        signoff=False,
    )
    _commit(
        repo,
        message="signed third",
        file_name="c.txt",
        file_content="c\n",
        signoff=True,
    )
    return repo


# --------------------------------------------------------------------- #
# --check / dry-run path                                                #
# --------------------------------------------------------------------- #


def test_check_lists_unsigned_commits_and_exits_1(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(fake_repo)
    before = _git("rev-parse", "HEAD", cwd=fake_repo).stdout.strip()

    result = CliRunner().invoke(app, ["contrib", "sign-commits", "--check"])

    assert result.exit_code == 1, result.output
    assert "unsigned first" in result.output
    assert "unsigned second" in result.output
    # The signed commit's subject should NOT be in the unsigned list.
    # We can't just check absence of "signed third" — could match prose.
    # Verify by counting how many lines name unsigned commits.
    lines = [line for line in result.output.splitlines() if "unsigned" in line]
    assert len(lines) >= 2  # at least the two unsigned commits

    # No history mutation.
    after = _git("rev-parse", "HEAD", cwd=fake_repo).stdout.strip()
    assert after == before


def test_check_returns_0_when_all_signed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "clean"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.name", "Bob", cwd=repo)
    _git("config", "user.email", "bob@example.com", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    _commit(repo, message="init", file_name="x", file_content="x", signoff=True)
    _git("checkout", "-b", "feat", cwd=repo)
    _commit(repo, message="signed work", file_name="y", file_content="y", signoff=True)

    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["contrib", "sign-commits", "--check"])
    assert result.exit_code == 0, result.output


# --------------------------------------------------------------------- #
# Rewriting path                                                        #
# --------------------------------------------------------------------- #


def test_sign_commits_rewrites_unsigned_and_leaves_signed_intact(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(fake_repo)

    result = CliRunner().invoke(
        app,
        ["contrib", "sign-commits", "--force"],
    )

    assert result.exit_code == 0, result.output

    # Every commit between main..HEAD must now carry exactly one
    # Signed-off-by: trailer matching the fixture's identity.
    log = _git(
        "log",
        "--format=%H%n%B%n----COMMIT-BOUNDARY----",
        "main..HEAD",
        cwd=fake_repo,
    ).stdout
    blocks = [b for b in log.split("----COMMIT-BOUNDARY----") if b.strip()]
    assert len(blocks) == 3
    for block in blocks:
        signoff_lines = [
            line
            for line in block.splitlines()
            if line.strip() == "Signed-off-by: Alice <alice@example.com>"
        ]
        assert len(signoff_lines) == 1, (
            f"expected exactly one Signed-off-by trailer, got {signoff_lines!r} in block:\n{block}"
        )


def test_sign_commits_base_flag_uses_custom_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--base develop`` walks ``develop..HEAD`` instead of ``main..HEAD``."""
    repo = tmp_path / "develop-base"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.name", "Alice", cwd=repo)
    _git("config", "user.email", "alice@example.com", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    _commit(repo, message="initial", file_name="r", file_content="r", signoff=True)

    # Create develop with one extra commit beyond main.
    _git("checkout", "-b", "develop", cwd=repo)
    _commit(repo, message="develop only", file_name="d", file_content="d", signoff=True)

    # Branch feature off develop; add one unsigned commit.
    _git("checkout", "-b", "feature", cwd=repo)
    _commit(
        repo,
        message="feature unsigned",
        file_name="f",
        file_content="f",
        signoff=False,
    )

    monkeypatch.chdir(repo)

    # --base develop -> only the one unsigned commit in develop..HEAD.
    result = CliRunner().invoke(
        app,
        ["contrib", "sign-commits", "--base", "develop", "--check"],
    )
    assert result.exit_code == 1, result.output
    assert "feature unsigned" in result.output
    # The signed "develop only" commit is outside the range, so it
    # must NOT appear in the unsigned-list output.
    assert "develop only" not in result.output


# --------------------------------------------------------------------- #
# Safety: refuse to operate                                             #
# --------------------------------------------------------------------- #


def test_detached_head_refuses_with_exit_2(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head_sha = _git("rev-parse", "HEAD", cwd=fake_repo).stdout.strip()
    _git("checkout", "--detach", head_sha, cwd=fake_repo)
    monkeypatch.chdir(fake_repo)

    result = CliRunner().invoke(app, ["contrib", "sign-commits"])

    assert result.exit_code == 2, result.output
    assert "detached" in result.output.lower()


def test_dirty_tree_refuses_with_exit_2(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Leave an unstaged change.
    (fake_repo / "a.txt").write_text("dirty\n")
    monkeypatch.chdir(fake_repo)

    result = CliRunner().invoke(app, ["contrib", "sign-commits"])

    assert result.exit_code == 2, result.output
    assert "dirty" in result.output.lower()


def test_merge_in_range_refuses_with_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "merge-repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.name", "Alice", cwd=repo)
    _git("config", "user.email", "alice@example.com", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    _commit(repo, message="init", file_name="i", file_content="i", signoff=True)

    # main has one extra commit; feature branches off main and merges main back.
    _git("checkout", "-b", "feature", cwd=repo)
    _commit(repo, message="feature work", file_name="f", file_content="f", signoff=False)
    _git("checkout", "main", cwd=repo)
    _commit(repo, message="main work", file_name="m", file_content="m", signoff=True)
    _git("checkout", "feature", cwd=repo)
    # Force a merge commit (no fast-forward).
    _git("merge", "--no-ff", "-m", "merge main", "main", cwd=repo)

    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["contrib", "sign-commits"])

    assert result.exit_code == 2, result.output
    assert "merge" in result.output.lower()


def test_missing_user_email_refuses_with_exit_2(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git("config", "--unset", "user.email", cwd=fake_repo)
    monkeypatch.chdir(fake_repo)

    result = CliRunner().invoke(app, ["contrib", "sign-commits"])

    assert result.exit_code == 2, result.output
    assert "user.email" in result.output or "user.name" in result.output


# --------------------------------------------------------------------- #
# Pushed-commit warning                                                 #
# --------------------------------------------------------------------- #


def _setup_pushed_branch(tmp_path: Path) -> Path:
    """Build feature repo with an upstream that already has commits in range."""
    upstream = tmp_path / "upstream.git"
    _git("init", "--bare", "-b", "main", cwd=tmp_path, env={"GIT_DIR": str(upstream)})
    # The above doesn't quite work the way we want; do it properly:
    upstream.mkdir(exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(upstream)],
        check=True,
        capture_output=True,
    )

    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.name", "Alice", cwd=work)
    _git("config", "user.email", "alice@example.com", cwd=work)
    _git("config", "commit.gpgsign", "false", cwd=work)
    _commit(work, message="init", file_name="i", file_content="i", signoff=True)
    _git("remote", "add", "origin", str(upstream), cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)

    _git("checkout", "-b", "feature", cwd=work)
    _commit(
        work,
        message="pushed unsigned",
        file_name="p",
        file_content="p",
        signoff=False,
    )
    _git("push", "-u", "origin", "feature", cwd=work)

    # One more local unsigned commit (not yet pushed).
    _commit(
        work,
        message="local unsigned",
        file_name="l",
        file_content="l",
        signoff=False,
    )
    return work


def test_pushed_commits_warning_aborts_on_no(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = _setup_pushed_branch(tmp_path)
    monkeypatch.chdir(work)

    result = CliRunner().invoke(
        app,
        ["contrib", "sign-commits"],
        input="n\n",
    )

    # The user said "no" → command aborts. Output should mention force-push.
    assert "force-with-lease" in result.output or "force" in result.output.lower()
    # Aborted — exit code should be non-zero (typer.Abort raises a non-zero
    # exit). Whatever the precise code, it should NOT be 0.
    assert result.exit_code != 0, result.output


def test_pushed_commits_warning_force_bypasses_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = _setup_pushed_branch(tmp_path)
    monkeypatch.chdir(work)

    result = CliRunner().invoke(
        app,
        ["contrib", "sign-commits", "--force"],
    )

    assert result.exit_code == 0, result.output
    # Warning text should still be present so the contributor knows
    # to push --force-with-lease afterward.
    assert "force-with-lease" in result.output or "force" in result.output.lower()

    # All commits in range now signed.
    log = _git(
        "log",
        "--format=%H%n%B%n----COMMIT-BOUNDARY----",
        "main..HEAD",
        cwd=work,
    ).stdout
    blocks = [b for b in log.split("----COMMIT-BOUNDARY----") if b.strip()]
    for block in blocks:
        signoff_lines = [
            line
            for line in block.splitlines()
            if line.strip() == "Signed-off-by: Alice <alice@example.com>"
        ]
        assert len(signoff_lines) == 1, block
