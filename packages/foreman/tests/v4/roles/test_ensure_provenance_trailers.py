"""Unit tests for :func:`foreman.roles.worker._ensure_provenance_trailers`.

Issue #347: the helper is the runtime backstop that catches the slip
case where the LLM's prompt-side :code:`<provenance_trailers>`
instruction failed to emit BOTH the ``Supervised-by:`` and
``Signed-off-by:`` trailers on the role's HEAD commit. Mirrors the
shape of the existing :func:`_sanitize_head_commit_auto_close`
helper — same single-commit / multi-commit / zero-commit branches.

These tests build a real worktree on ``tmp_path``, drive the helper
directly, and assert on ``git log -1 --pretty=%B`` so the contract
is pinned end-to-end (helper → real ``git commit --amend`` → ground
truth).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from foreman.roles.worker import _ensure_provenance_trailers
from foreman.v4.config import OperatorConfig, OperatorIdentity


def _init_worktree(tmp_path: Path) -> Path:
    """Init a bare-bones git repo + a single seed commit for use as a worktree."""
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "seed@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Seed"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _operator() -> OperatorConfig:
    return OperatorConfig(
        supervisor=OperatorIdentity(name="Wren Richley", email="wren@example.com"),
        signer=OperatorIdentity(name="Jeff Richley", email="jeff@example.com"),
    )


def _head_body(worktree_path: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_zero_commits_no_op(tmp_path: Path) -> None:
    """commits_made_count == 0 → no-op (returns False)."""
    wt = _init_worktree(tmp_path)
    before = _head_body(wt)
    result = _ensure_provenance_trailers(
        worktree_path=wt,
        operator=_operator(),
        commits_made_count=0,
        role_token="ghp_fake",
    )
    assert result is False
    assert _head_body(wt) == before


def test_single_commit_both_missing_amends(tmp_path: Path) -> None:
    """commits_made_count == 1 with NEITHER trailer present → both added."""
    wt = _init_worktree(tmp_path)
    result = _ensure_provenance_trailers(
        worktree_path=wt,
        operator=_operator(),
        commits_made_count=1,
        role_token="ghp_fake",
    )
    assert result is True
    body = _head_body(wt)
    assert "Supervised-by: Wren Richley <wren@example.com>" in body
    assert "Signed-off-by: Jeff Richley <jeff@example.com>" in body


def test_single_commit_only_supervisor_missing_adds_only_supervisor(
    tmp_path: Path,
) -> None:
    """commits_made_count == 1 with only Signed-off-by present → only
    Supervised-by added; the existing Signed-off-by stays put."""
    wt = _init_worktree(tmp_path)
    # Pre-seed Signed-off-by via an amend.
    subprocess.run(
        [
            "git",
            "commit",
            "--amend",
            "--no-edit",
            "--trailer",
            "Signed-off-by: Jeff Richley <jeff@example.com>",
        ],
        cwd=wt,
        check=True,
        capture_output=True,
    )
    result = _ensure_provenance_trailers(
        worktree_path=wt,
        operator=_operator(),
        commits_made_count=1,
        role_token="ghp_fake",
    )
    assert result is True
    body = _head_body(wt)
    assert "Supervised-by: Wren Richley <wren@example.com>" in body
    # Signed-off-by appears exactly once (no duplicate from re-emit).
    assert body.count("Signed-off-by: Jeff Richley <jeff@example.com>") == 1


def test_single_commit_only_signer_missing_adds_only_signer(tmp_path: Path) -> None:
    """commits_made_count == 1 with only Supervised-by present → only
    Signed-off-by added; the existing Supervised-by stays put."""
    wt = _init_worktree(tmp_path)
    subprocess.run(
        [
            "git",
            "commit",
            "--amend",
            "--no-edit",
            "--trailer",
            "Supervised-by: Wren Richley <wren@example.com>",
        ],
        cwd=wt,
        check=True,
        capture_output=True,
    )
    result = _ensure_provenance_trailers(
        worktree_path=wt,
        operator=_operator(),
        commits_made_count=1,
        role_token="ghp_fake",
    )
    assert result is True
    body = _head_body(wt)
    assert "Signed-off-by: Jeff Richley <jeff@example.com>" in body
    assert body.count("Supervised-by: Wren Richley <wren@example.com>") == 1


def test_single_commit_both_present_no_op(tmp_path: Path) -> None:
    """commits_made_count == 1 with BOTH trailers already present → no-op."""
    wt = _init_worktree(tmp_path)
    subprocess.run(
        [
            "git",
            "commit",
            "--amend",
            "--no-edit",
            "--trailer",
            "Supervised-by: Wren Richley <wren@example.com>",
            "--trailer",
            "Signed-off-by: Jeff Richley <jeff@example.com>",
        ],
        cwd=wt,
        check=True,
        capture_output=True,
    )
    before_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    result = _ensure_provenance_trailers(
        worktree_path=wt,
        operator=_operator(),
        commits_made_count=1,
        role_token="ghp_fake",
    )
    assert result is False
    after_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # No amend means the SHA didn't shift.
    assert before_sha == after_sha


def test_multi_commit_warn_and_skip(tmp_path: Path) -> None:
    """commits_made_count > 1 → warn-and-skip; HEAD unchanged."""
    wt = _init_worktree(tmp_path)
    before = _head_body(wt)
    before_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    result = _ensure_provenance_trailers(
        worktree_path=wt,
        operator=_operator(),
        commits_made_count=2,
        role_token="ghp_fake",
    )
    assert result is False
    after = _head_body(wt)
    after_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert before == after
    assert before_sha == after_sha
