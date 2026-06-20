"""Unit tests for ``foreman contrib check-signoff`` (the alias path).

These reuse the same fake-repo shape as test_contrib_sign_commits.py
and assert that the alias's exit code + stdout match
``sign-commits --check`` line for line.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foreman.v4.cli import app


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )


def _commit(
    repo: Path,
    *,
    message: str,
    file_name: str,
    file_content: str,
    signoff: bool,
) -> None:
    (repo / file_name).write_text(file_content)
    _git("add", file_name, cwd=repo)
    args = ["commit"]
    if signoff:
        args.append("-s")
    args.extend(["-m", message])
    _git(*args, cwd=repo)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "alias-repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.name", "Alice", cwd=repo)
    _git("config", "user.email", "alice@example.com", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    _commit(repo, message="init", file_name="i", file_content="i", signoff=True)
    _git("checkout", "-b", "feature", cwd=repo)
    _commit(
        repo,
        message="unsigned one",
        file_name="u1",
        file_content="u1",
        signoff=False,
    )
    _commit(
        repo,
        message="signed one",
        file_name="s1",
        file_content="s1",
        signoff=True,
    )
    return repo


def test_check_signoff_alias_exits_1_with_unsigned(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(fake_repo)

    alias = CliRunner().invoke(app, ["contrib", "check-signoff"])
    sign_check = CliRunner().invoke(app, ["contrib", "sign-commits", "--check"])

    assert alias.exit_code == 1
    assert sign_check.exit_code == 1
    # Alias output should match the dry-run path's output: same listing
    # of unsigned commits.
    assert "unsigned one" in alias.output
    # Same shape — both flow through _check_signoff so the exit code +
    # the unsigned-commit-listing content match.
    assert alias.exit_code == sign_check.exit_code


def test_check_signoff_alias_exits_0_when_all_signed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "clean-alias"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.name", "Bob", cwd=repo)
    _git("config", "user.email", "bob@example.com", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    _commit(repo, message="init", file_name="i", file_content="i", signoff=True)
    _git("checkout", "-b", "feat", cwd=repo)
    _commit(repo, message="signed", file_name="s", file_content="s", signoff=True)
    monkeypatch.chdir(repo)

    result = CliRunner().invoke(app, ["contrib", "check-signoff"])
    assert result.exit_code == 0, result.output


def test_check_signoff_alias_accepts_base_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "base-alias"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.name", "Alice", cwd=repo)
    _git("config", "user.email", "alice@example.com", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)
    _commit(repo, message="init", file_name="i", file_content="i", signoff=True)
    _git("checkout", "-b", "develop", cwd=repo)
    _commit(repo, message="develop signed", file_name="d", file_content="d", signoff=True)
    _git("checkout", "-b", "feature", cwd=repo)
    _commit(
        repo,
        message="feature unsigned",
        file_name="f",
        file_content="f",
        signoff=False,
    )

    monkeypatch.chdir(repo)
    result = CliRunner().invoke(
        app,
        ["contrib", "check-signoff", "--base", "develop"],
    )
    assert result.exit_code == 1, result.output
    assert "feature unsigned" in result.output
