"""Tests for per-ticket git worktree create + cleanup."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from foreman.worktree import (
    WorktreeManager,
    _fetch_origin_branch,
    _origin_branch_exists,
    fetch_origin_default_branch,
)


def _init_git_repo(
    path: Path,
    *,
    with_pyproject: bool = False,
    origin_path: Path | None = None,
) -> None:
    """Initialize a minimal git repo with one commit so we can branch from it.

    If ``origin_path`` is provided, a bare upstream is created there, wired
    as ``origin``, and the seed commit is pushed so ``origin/main`` exists
    and ``refs/remotes/origin/HEAD`` resolves. ``WorktreeManager.create``
    bases new branches on ``origin/<default>`` rather than local HEAD, so
    tests that exercise ``create`` need an origin set up.
    """
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    if with_pyproject:
        (path / "pyproject.toml").write_text("[project]\nname = 'fake'\nversion = '0.0.0'\n")
        subprocess.run(["git", "add", "pyproject.toml"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=path, check=True, capture_output=True)
    if origin_path is not None:
        _wire_origin(clone=path, origin=origin_path)


def _wire_origin(*, clone: Path, origin: Path) -> None:
    """Create a bare repo at ``origin``, wire it as ``origin`` on ``clone``,
    push ``main``, and set ``refs/remotes/origin/HEAD`` so
    ``_resolve_default_branch`` can find the default branch.
    """
    origin.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "main"],
        cwd=origin,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "set-head", "origin", "main"],
        cwd=clone,
        check=True,
        capture_output=True,
    )


def test_fetch_origin_branch_prunes_stale_ref_when_remote_branch_is_gone(
    tmp_path: Path,
) -> None:
    """foreman#122: when a spec branch was deleted on origin (e.g.,
    spec PR merged + GitHub auto-delete), the local
    ``refs/remotes/origin/<branch>`` ref still points at the prior tip
    until something prunes it. Without the prune, the WorktreeManager
    base-branch fallback reads the cached ref, returns ``foreman/
    issue-N`` as the base, and ``create_pull`` faceplants on 422
    "base invalid." This test pins the prune-on-couldnt-find-remote-ref
    behavior in ``_fetch_origin_branch``.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    origin_path = tmp_path / "origin.git"
    _init_git_repo(clone, origin_path=origin_path)

    branch = "foreman/issue-122"
    # Create the spec branch locally + push to origin so the local
    # `refs/remotes/origin/foreman/issue-122` ref exists.
    subprocess.run(
        ["git", "checkout", "-b", branch],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    (clone / "spec.md").write_text("# stub spec\n")
    subprocess.run(
        ["git", "add", "spec.md"], cwd=clone, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "stub spec"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", branch], cwd=clone, check=True, capture_output=True
    )
    # Confirm the precondition: local origin ref resolves.
    assert _origin_branch_exists(clone, branch) is True

    # Simulate GitHub auto-deleting the branch on origin AFTER PR merge.
    # The deletion happens server-side, NOT via `git push origin --delete`
    # from this client — so the client's local refs/remotes/origin/<branch>
    # stays stale until the next fetch fails. We replicate that by
    # mutating the bare origin repo directly.
    subprocess.run(
        ["git", "update-ref", "-d", f"refs/heads/{branch}"],
        cwd=origin_path,
        check=True,
        capture_output=True,
    )
    # The client's local cache STILL says the branch exists — that's
    # the precondition for the bug.
    assert _origin_branch_exists(clone, branch) is True

    # The fix: `_fetch_origin_branch` now detects "couldn't find remote
    # ref" in the fetch's stderr and prunes the stale local ref.
    _fetch_origin_branch(clone, branch)

    # Postcondition: the stale local ref is gone, so the WorktreeManager's
    # `_origin_branch_exists` check returns False and the fallback gate
    # to the default branch can fire.
    assert _origin_branch_exists(clone, branch) is False


def test_create_worktree_creates_dir_with_branch(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, origin_path=tmp_path / "origin.git")

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert wt_path.exists()
    assert wt_path == worktrees_root / "voice" / "issue-42"
    branch_check = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch_check.stdout.strip() == "foreman/issue-42"


def test_cleanup_removes_worktree(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, origin_path=tmp_path / "origin.git")

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)
    assert wt_path.exists()

    mgr.cleanup(clone_path=clone, worktree_path=wt_path)
    assert not wt_path.exists()


def test_create_idempotent_on_existing_worktree(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, origin_path=tmp_path / "origin.git")

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt1 = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)
    wt2 = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)
    assert wt1 == wt2, "Re-creating same worktree should return existing path"


# ----------------------------------------------------------------------
# Issue #10 follow-up — pre-populate the worktree's venv via ``uv sync``
#
# A target repo's pre-push hook runs ``uv run --no-sync just check``. The
# ``--no-sync`` flag means the hook will NOT install deps on its own — it
# expects an already-populated ``.venv``. Worktrees share git history with
# the clone but NOT gitignored files like ``.venv``, so a freshly-created
# worktree has no venv at all. Without intervention, ``uv run --no-sync``
# creates a fresh empty venv and mypy/pytest then fail because the venv
# has no deps installed.
#
# Fix: ``WorktreeManager.create`` runs ``uv sync --all-packages`` in the
# new worktree when a ``pyproject.toml`` is present at root and ``uv`` is
# on PATH. The sync uses the same env filter as ``GitHubProvider._git``
# so we don't re-introduce the env-leak this whole effort fixed.
# ----------------------------------------------------------------------


def _capture_uv_sync_run(
    *, capture: dict[str, Any], outcome: subprocess.CompletedProcess[str] | None = None
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a fake ``subprocess.run`` that records ``uv sync`` calls and
    falls through to real ``subprocess.run`` for everything else (git ops).

    If ``outcome`` is supplied, that result is returned for the ``uv sync``
    call instead of a default success.
    """
    real_run = subprocess.run

    def fake_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0] == "uv" and cmd[1] == "sync":
            capture["cmd"] = cmd
            capture["cwd"] = kwargs.get("cwd")
            capture["env"] = kwargs.get("env")
            capture["check"] = kwargs.get("check", False)
            capture["capture_output"] = kwargs.get("capture_output", False)
            if outcome is not None:
                if kwargs.get("check") and outcome.returncode != 0:
                    raise subprocess.CalledProcessError(
                        outcome.returncode,
                        outcome.args,
                        output=outcome.stdout,
                        stderr=outcome.stderr,
                    )
                return outcome
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="Resolved 3 packages\nInstalled 3 packages\n",
                stderr="",
            )
        return real_run(cmd, *args, **kwargs)

    return fake_run


def test_create_runs_uv_sync_if_pyproject_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the worktree has a ``pyproject.toml`` at root and ``uv`` is on
    PATH, ``WorktreeManager.create`` runs ``uv sync --all-packages`` in
    the worktree to pre-populate its venv for the pre-push hook."""
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, with_pyproject=True, origin_path=tmp_path / "origin.git")

    monkeypatch.setattr("foreman.worktree.shutil.which", lambda exe: "/fake/path/uv")

    capture: dict[str, Any] = {}
    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)

    with patch(
        "foreman.worktree.subprocess.run",
        side_effect=_capture_uv_sync_run(capture=capture),
    ):
        wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert wt_path.exists()
    assert capture.get("cmd") == ["uv", "sync", "--all-packages"], (
        f"Expected uv sync --all-packages, got {capture.get('cmd')!r}"
    )
    assert capture.get("cwd") == wt_path, (
        f"Expected uv sync cwd={wt_path}, got {capture.get('cwd')!r}"
    )


def test_create_skips_uv_sync_if_no_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the worktree has no ``pyproject.toml`` at root, skip ``uv sync``
    entirely — some target repos don't use uv (or aren't Python at all)."""
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, with_pyproject=False, origin_path=tmp_path / "origin.git")

    monkeypatch.setattr("foreman.worktree.shutil.which", lambda exe: "/fake/path/uv")

    capture: dict[str, Any] = {}
    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)

    with patch(
        "foreman.worktree.subprocess.run",
        side_effect=_capture_uv_sync_run(capture=capture),
    ):
        wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert wt_path.exists()
    assert "cmd" not in capture, (
        "uv sync must not run when the worktree has no pyproject.toml at root"
    )


def test_create_skips_uv_sync_if_uv_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``uv`` isn't installed, skip the sync — better to let the hook
    fail loudly with a clear ``uv: not found`` than to crash worktree creation."""
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, with_pyproject=True, origin_path=tmp_path / "origin.git")

    monkeypatch.setattr("foreman.worktree.shutil.which", lambda exe: None)

    capture: dict[str, Any] = {}
    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)

    with patch(
        "foreman.worktree.subprocess.run",
        side_effect=_capture_uv_sync_run(capture=capture),
    ):
        wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert wt_path.exists()
    assert "cmd" not in capture, "uv sync must not run when uv is not on PATH"


def test_create_swallows_uv_sync_failure_but_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If ``uv sync`` fails, the worktree must still be returned — the
    push step will then fail loudly with a clear hook error, which is a
    better signal to the operator than a worktree-create crash that
    masks the real problem."""
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, with_pyproject=True, origin_path=tmp_path / "origin.git")

    monkeypatch.setattr("foreman.worktree.shutil.which", lambda exe: "/fake/path/uv")

    failure = subprocess.CompletedProcess(
        args=["uv", "sync", "--all-packages"],
        returncode=1,
        stdout="",
        stderr="error: no compatible Python interpreter found",
    )

    capture: dict[str, Any] = {}
    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)

    with patch(
        "foreman.worktree.subprocess.run",
        side_effect=_capture_uv_sync_run(capture=capture, outcome=failure),
    ):
        wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert wt_path.exists(), "worktree must still be created when uv sync fails"
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "uv sync" in combined and "warn" in combined.lower(), (
        f"Expected a warning mentioning uv sync, got stdout={captured.out!r} "
        f"stderr={captured.err!r}"
    )
    assert "no compatible Python interpreter found" in combined, (
        "Warning must include uv sync stderr so the operator can diagnose"
    )


def test_create_filters_env_for_uv_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``uv sync`` runs with the same env filter as :class:`GitHubProvider._git`:
    Foreman's ``VIRTUAL_ENV`` etc. must NOT leak into the foreign worktree's
    sync — otherwise we re-introduce the exact bug that issue #10 fixed."""
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, with_pyproject=True, origin_path=tmp_path / "origin.git")

    monkeypatch.setattr("foreman.worktree.shutil.which", lambda exe: "/fake/path/uv")
    monkeypatch.setenv("VIRTUAL_ENV", "sentinel-do-not-leak")
    monkeypatch.setenv("PYTHONPATH", "sentinel-pythonpath")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "sentinel-uv-project-env")

    capture: dict[str, Any] = {}
    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)

    with patch(
        "foreman.worktree.subprocess.run",
        side_effect=_capture_uv_sync_run(capture=capture),
    ):
        mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    env = capture.get("env")
    assert env is not None, "uv sync must receive an explicit env= so the filter applies"
    assert "VIRTUAL_ENV" not in env, (
        "VIRTUAL_ENV leaked into uv sync env — would re-introduce issue #10"
    )
    assert "PYTHONPATH" not in env
    assert "UV_PROJECT_ENVIRONMENT" not in env


# ----------------------------------------------------------------------
# Drift fix — base new spec branches on ``origin/<default>``, not local HEAD.
#
# Real-world case: voice's local ``main`` carried commit ``0200159``
# (release-pipeline work that had already shipped via PR #7 as a different
# SHA ``e64df33``). Every Foreman spec PR (#11, #15, #16, #17) inherited
# that drift as "extra commits" unrelated to the spec, and the Reviewer's
# drift rule (23750f6) caught it at review time. Root cause is HERE: the
# Planner created its per-ticket branch from whatever the clone's HEAD
# happened to be. The fix bases the new branch on the freshly-fetched
# ``origin/<default-branch>`` instead, isolating spec PRs from local drift.
# ----------------------------------------------------------------------


def test_create_bases_new_branch_on_origin_not_local_head(tmp_path: Path) -> None:
    """If local ``main`` is ahead of ``origin/main`` (drift), the new
    ``foreman/issue-<N>`` branch must be based on ``origin/main``, NOT the
    drifted local tip — otherwise every spec PR inherits the drift as
    "extra commits" unrelated to the spec.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, origin_path=tmp_path / "origin.git")

    # Record the origin tip (this is where the new branch SHOULD be based)
    origin_tip = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Simulate drift: add a commit to local main that isn't on origin.
    # In the real voice case this was 0200159 — release-pipeline work that
    # had already shipped as a different SHA via squash-merge.
    (clone / "drift.txt").write_text("local drift not on origin\n")
    subprocess.run(["git", "add", "drift.txt"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "drift: unshipped local commit"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    local_tip = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert local_tip != origin_tip, "test setup: local must be ahead of origin"

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=99)

    # The new branch tip MUST equal origin/main, NOT the drifted local tip.
    branch_tip = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch_tip == origin_tip, (
        f"New spec branch must be based on origin/main ({origin_tip}), "
        f"not local drifted HEAD ({local_tip}). Got {branch_tip}."
    )
    assert branch_tip != local_tip, (
        "Sanity: branch tip must not equal local drifted HEAD — that would "
        "re-introduce the voice 0200159 drift bug."
    )


def test_create_resolves_default_branch_from_origin_head(tmp_path: Path) -> None:
    """The default branch is read dynamically from
    ``refs/remotes/origin/HEAD``, not hardcoded as ``main``. Repos that use
    ``master`` (or any other default) must still work.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    # Init on master to prove we don't assume "main".
    subprocess.run(["git", "init", "-b", "master"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    (clone / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "README.md"], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=clone, check=True, capture_output=True)
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "master"],
        cwd=origin,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "push", "origin", "master"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "set-head", "origin", "master"],
        cwd=clone,
        check=True,
        capture_output=True,
    )

    origin_tip = subprocess.run(
        ["git", "rev-parse", "origin/master"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=7)

    branch_tip = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch_tip == origin_tip, (
        "Default branch must be resolved from origin/HEAD (master here), not hardcoded as main."
    )


def test_create_continues_when_fetch_fails_offline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If ``git fetch origin <default>`` fails (e.g., network down), the
    worktree create must still proceed — falling back to the cached origin
    ref. A hard failure here would block ticket execution on transient
    connectivity issues. The failure must surface as a stderr warning so
    operators can diagnose.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, origin_path=tmp_path / "origin.git")

    # Break the origin remote URL so fetch fails fast. We keep
    # ``refs/remotes/origin/main`` (cached from the initial push) so
    # worktree add still has a valid revision to base on.
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")],
        cwd=clone,
        check=True,
        capture_output=True,
    )

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert wt_path.exists(), "worktree must still be created when fetch fails offline"
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "git fetch" in combined and "warn" in combined.lower(), (
        f"Expected a stderr warning about git fetch failure, "
        f"got stdout={captured.out!r} stderr={captured.err!r}"
    )


def test_create_falls_back_to_main_when_origin_head_unset(tmp_path: Path) -> None:
    """If ``refs/remotes/origin/HEAD`` is missing (some clones don't have
    it), ``_resolve_default_branch`` must fall back to ``main`` rather
    than crash. We prove the fallback works by un-setting the symbolic
    ref after the test fixture sets it up.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, origin_path=tmp_path / "origin.git")

    # Strip the symbolic ref to simulate a clone where origin/HEAD was
    # never set (e.g., a clone created with --no-checkout or hand-built).
    subprocess.run(
        ["git", "symbolic-ref", "--delete", "refs/remotes/origin/HEAD"],
        cwd=clone,
        check=True,
        capture_output=True,
    )

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    # Should not raise — falls back to "main", which exists on origin.
    wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)
    assert wt_path.exists()


def test_create_filters_env_on_git_subprocess_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every git subprocess inside ``create`` (symbolic-ref, fetch,
    worktree add) must receive the same env filter as ``uv sync`` —
    otherwise we'd re-leak ``VIRTUAL_ENV`` into git hooks running in the
    foreign worktree. Pins ``env=filtered_subprocess_env()`` on each call.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, origin_path=tmp_path / "origin.git")

    monkeypatch.setenv("VIRTUAL_ENV", "sentinel-do-not-leak")

    real_run = subprocess.run
    captured_envs: list[dict[str, str] | None] = []

    def recording_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            captured_envs.append(kwargs.get("env"))
        return real_run(cmd, *args, **kwargs)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)

    with patch("foreman.worktree.subprocess.run", side_effect=recording_run):
        mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert captured_envs, "expected at least one git subprocess call from create()"
    for env in captured_envs:
        assert env is not None, (
            "every git call in create() must pass env=filtered_subprocess_env(); "
            "default env=None would inherit VIRTUAL_ENV and re-introduce issue #10"
        )
        assert "VIRTUAL_ENV" not in env, "VIRTUAL_ENV leaked into a git subprocess inside create()"


# ----------------------------------------------------------------------
# create_impl — Worker's stacked impl branch
#
# create_impl mirrors ``create`` but the new branch is ``foreman/impl-<N>``
# based on ``origin/foreman/issue-<N>`` (the spec branch the Planner /
# Reviewer / Fixer iterate on). The Worker's PR is opened with
# base=spec-branch so the spec PR stays independently reviewable; the
# orchestrator retargets the impl PR to the repo default when the spec
# PR merges (out of scope here).
# ----------------------------------------------------------------------


def _seed_clone_with_spec_branch_pushed(clone: Path, *, ticket_id: int) -> str:
    """Like the fixer test fixture, but pushes ``foreman/issue-<N>`` to a
    bare upstream so ``create_impl`` can resolve ``origin/foreman/issue-<N>``.

    Returns the spec-branch head SHA so callers can verify the new impl
    branch is based on it.
    """
    clone.mkdir()
    _init_git_repo(clone, origin_path=clone.parent / "origin.git")
    spec_branch = f"foreman/issue-{ticket_id}"
    subprocess.run(
        ["git", "checkout", "-b", spec_branch], cwd=clone, check=True, capture_output=True
    )
    spec_dir = clone / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / f"foreman-issue-{ticket_id}-spec.md").write_text(f"# Spec for issue #{ticket_id}\n")
    subprocess.run(["git", "add", "."], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "spec doc"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "push", "origin", spec_branch], cwd=clone, check=True, capture_output=True
    )
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "main"], cwd=clone, check=True, capture_output=True)
    return head_sha


def test_create_impl_creates_dir_with_stacked_branch(tmp_path: Path) -> None:
    """``create_impl`` builds ``foreman/impl-<N>`` based on the spec branch,
    in a sibling worktree directory ``impl-<N>``."""
    clone = tmp_path / "clone"
    spec_head = _seed_clone_with_spec_branch_pushed(clone, ticket_id=42)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    result = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)
    wt_path = result.path

    assert wt_path.exists()
    assert wt_path == worktrees_root / "voice" / "impl-42"

    branch_check = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch_check.stdout.strip() == "foreman/impl-42"

    # The new impl branch must be based on the spec branch's tip — that's
    # the stacked-PR invariant the Worker relies on.
    impl_tip = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert impl_tip == spec_head, (
        f"impl-<N> branch must be based on spec branch tip ({spec_head}); got {impl_tip}"
    )


def test_create_impl_is_idempotent_on_existing_path(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    _seed_clone_with_spec_branch_pushed(clone, ticket_id=42)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt1 = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)
    wt2 = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)
    assert wt1.path == wt2.path


def test_create_impl_separate_from_spec_worktree(tmp_path: Path) -> None:
    """``impl-<N>`` and ``issue-<N>`` are sibling worktrees, NEVER the same
    dir — the Worker must not inherit a Fixer's WIP spec edits."""
    clone = tmp_path / "clone"
    _seed_clone_with_spec_branch_pushed(clone, ticket_id=42)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    impl_wt = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42).path
    spec_wt = mgr.attach(clone_path=clone, repo_slug="voice", ticket_id=42)
    assert impl_wt != spec_wt
    assert impl_wt.name == "impl-42"
    assert spec_wt.name == "issue-42"


def test_create_impl_self_heals_after_container_rebuild(tmp_path: Path) -> None:
    """foreman#220: simulate the stranded-state-after-container-rebuild
    scenario that surfaced on foreman#170.

    Before rebuild: ``create_impl`` ran, ``foreman/impl-42`` was created
    locally with a worktree dir on the container's ephemeral filesystem.

    Rebuild: the worktree dir was wiped (it lived on the container's
    ephemeral fs, NOT on the persistent `foreman-repos` volume), but
    the clone's `.git/worktrees/impl-42/` metadata file and the local
    ``foreman/impl-42`` branch survived because they live in the
    persistent clone.

    After rebuild: a fresh ``create_impl`` call hits
    ``git worktree add -b foreman/impl-42 ...`` and git refuses with
    exit 128 because the branch name is taken locally. Without
    self-heal, the Worker burns an impl-attempt budget slot on
    infrastructure noise.

    The self-heal contract: prune stale worktree metadata + delete the
    orphan local branch BEFORE the worktree add, so this scenario
    recovers transparently.
    """
    clone = tmp_path / "clone"
    _seed_clone_with_spec_branch_pushed(clone, ticket_id=42)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)

    # Simulate the rebuild-leftover state: create the worktree on the
    # first pass (this stamps both the branch and the metadata file),
    # then physically delete the worktree dir to imitate the wiped
    # ephemeral filesystem post-rebuild. The clone's `.git/worktrees/`
    # entry is now stale and the local branch is orphaned.
    first_result = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)
    import shutil

    shutil.rmtree(first_result.path)
    # Sanity: the local branch and stale metadata should still exist.
    branch_list = subprocess.run(
        ["git", "branch", "--list", "foreman/impl-42"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "foreman/impl-42" in branch_list.stdout, (
        "test precondition failed: orphan branch missing — the simulation "
        "does not reflect the foreman#170 stranded state"
    )

    # Self-heal: fresh create_impl call MUST recover (this is the failure
    # mode foreman#170 hit in production today on 2026-06-07).
    result = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert result.path.exists()
    # After recovery the worktree should be checked out on
    # ``foreman/impl-42`` again — the branch was recreated freshly off
    # the spec branch tip.
    branch_check = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=result.path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch_check.stdout.strip() == "foreman/impl-42"


def test_create_self_heals_after_container_rebuild(tmp_path: Path) -> None:
    """foreman#220: same stranded-state recovery contract, for the
    ``create`` (spec / planning) worktree path. The
    ``foreman/issue-<N>`` branch and worktree metadata strand the
    same way after a container rebuild — Planner and Reviewer hit
    this on a fresh container after the spec PR has been merged
    once but the issue retains its plan-approved label and gets
    re-dispatched.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, origin_path=clone.parent / "origin.git")

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)

    # First pass: creates issue-42 worktree + foreman/issue-42 branch.
    first_wt = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    import shutil

    shutil.rmtree(first_wt)
    branch_list = subprocess.run(
        ["git", "branch", "--list", "foreman/issue-42"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "foreman/issue-42" in branch_list.stdout

    # Self-heal: fresh create MUST recover.
    wt = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert wt.exists()
    branch_check = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch_check.stdout.strip() == "foreman/issue-42"


def test_create_impl_filters_env_on_git_subprocess_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every git subprocess inside ``create_impl`` must use the env filter —
    same VIRTUAL_ENV leak protection as ``create``."""
    clone = tmp_path / "clone"
    _seed_clone_with_spec_branch_pushed(clone, ticket_id=42)

    monkeypatch.setenv("VIRTUAL_ENV", "sentinel-do-not-leak")

    real_run = subprocess.run
    captured_envs: list[dict[str, str] | None] = []

    def recording_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            captured_envs.append(kwargs.get("env"))
        return real_run(cmd, *args, **kwargs)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)

    with patch("foreman.worktree.subprocess.run", side_effect=recording_run):
        mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert captured_envs, "expected at least one git subprocess call from create_impl"
    for env in captured_envs:
        assert env is not None, (
            "every git call in create_impl must pass env=filtered_subprocess_env(); "
            "default env=None would inherit VIRTUAL_ENV and re-leak the fix"
        )
        assert "VIRTUAL_ENV" not in env


# ----------------------------------------------------------------------
# create_impl — fallback to default branch when spec branch missing (issue #48)
#
# The spec branch ``origin/foreman/issue-<N>`` may be missing by the time
# the Worker runs: spec PR merged with ``--delete-branch``, "Automatically
# delete head branches" enabled on the repo, or an operator deleted it
# manually between Reviewer and Worker. In that case, if the spec doc
# landed on the default branch, ``create_impl`` falls back to branching
# off ``origin/<default>`` and reports that as the impl PR's base. If the
# spec doc is ALSO missing from default, the fallback aborts loudly
# rather than silently branching from an unrelated default.
# ----------------------------------------------------------------------


def _seed_clone_with_spec_doc_on_default_only(clone: Path, *, ticket_id: int) -> str:
    """Init a clone whose ``origin/main`` carries the spec doc but where
    ``foreman/issue-<N>`` was never pushed (or has been deleted).

    Returns the ``origin/main`` tip SHA after the spec doc commit so
    callers can verify the new impl branch's tip matches.
    """
    clone.mkdir()
    _init_git_repo(clone, origin_path=clone.parent / "origin.git")
    # Add the spec doc on main and push so origin/main carries it.
    spec_dir = clone / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / f"foreman-issue-{ticket_id}-spec.md").write_text(f"# Spec for issue #{ticket_id}\n")
    subprocess.run(["git", "add", "."], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "docs: spec for issue"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "push", "origin", "main"], cwd=clone, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_create_impl_falls_back_to_default_when_spec_branch_missing(tmp_path: Path) -> None:
    """When ``origin/foreman/issue-<N>`` is gone but the spec doc landed on
    ``origin/main``, ``create_impl`` branches the new impl worktree off
    ``origin/main`` and reports ``base_branch="main"`` so the impl PR
    can open with the correct base from the start. No stacking, no
    retarget step needed."""
    clone = tmp_path / "clone"
    origin_main_tip = _seed_clone_with_spec_doc_on_default_only(clone, ticket_id=42)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    result = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert result.path.exists()
    assert result.base_branch == "main", (
        f"Fallback path must report base_branch='main' so the impl PR "
        f"opens with the right base; got {result.base_branch!r}"
    )

    branch_check = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=result.path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch_check.stdout.strip() == "foreman/impl-42"

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=result.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == origin_main_tip, (
        f"Fallback impl branch must be based on origin/main's tip "
        f"({origin_main_tip}); got {head}"
    )


def test_create_impl_prefers_spec_branch_when_present(tmp_path: Path) -> None:
    """If BOTH the spec branch and the spec doc on default are present
    (in-flight pipeline before spec PR merge), the stacked path remains
    preferred — ``base_branch == "foreman/issue-<N>"`` and the worktree
    tip matches the spec branch."""
    clone = tmp_path / "clone"
    spec_head = _seed_clone_with_spec_branch_pushed(clone, ticket_id=42)
    # The seed already places the spec doc on the spec branch; merge it
    # onto main so origin/main also carries the spec doc, simulating
    # the "spec PR already merged but its branch wasn't deleted" state.
    subprocess.run(
        ["git", "merge", "--ff-only", "foreman/issue-42"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "push", "origin", "main"], cwd=clone, check=True, capture_output=True)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    result = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert result.base_branch == "foreman/issue-42", (
        "When both the spec branch and the spec doc on default exist, "
        "the stacked path must be preferred — base_branch should be "
        f"'foreman/issue-42', got {result.base_branch!r}"
    )

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=result.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == spec_head, (
        f"Stacked impl branch must be based on the spec branch's tip "
        f"({spec_head}); got {head}"
    )


def test_create_impl_raises_when_neither_spec_branch_nor_spec_doc_on_default(
    tmp_path: Path,
) -> None:
    """Both probes failing → loud, actionable RuntimeError citing the
    missing pieces and issue #48 — strictly better than the deep
    ``subprocess.CalledProcessError`` ``git worktree add`` would raise."""
    clone = tmp_path / "clone"
    clone.mkdir()
    # Plain clone with origin/main but NO spec doc and NO spec branch.
    _init_git_repo(clone, origin_path=tmp_path / "origin.git")

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)

    with pytest.raises(RuntimeError) as exc_info:
        mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)

    message = str(exc_info.value)
    assert "foreman/issue-42" in message, (
        f"Error must name the missing spec branch; got {message!r}"
    )
    assert "main" in message, f"Error must name the default branch checked; got {message!r}"
    assert "docs/superpowers/specs/foreman-issue-42-spec.md" in message, (
        f"Error must name the expected spec doc path; got {message!r}"
    )
    assert "#48" in message, (
        f"Error must reference issue #48 so the next reader lands on "
        f"the design notes; got {message!r}"
    )


def test_create_impl_idempotent_returns_result_with_recomputed_base(
    tmp_path: Path,
) -> None:
    """On idempotent re-call (impl worktree path already exists), the
    returned ``base_branch`` is recomputed from current origin state —
    NOT cached from the first call. This matters for crash-recovery
    after the spec PR merges + ``--delete-branch`` between the first
    ``create_impl`` and the second."""
    clone = tmp_path / "clone"
    spec_head = _seed_clone_with_spec_branch_pushed(clone, ticket_id=42)
    # Also publish the spec doc on origin/main so the fallback path is
    # viable later, when we simulate the spec branch being deleted.
    subprocess.run(
        ["git", "merge", "--ff-only", "foreman/issue-42"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "push", "origin", "main"], cwd=clone, check=True, capture_output=True)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)

    # First call: spec branch present → stacked path
    first = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)
    assert first.base_branch == "foreman/issue-42"
    assert first.path.exists()

    # Sanity: confirm first call branched off the spec head.
    first_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=first.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert first_head == spec_head

    # Simulate spec PR merge + ``--delete-branch``: delete the spec
    # branch from the bare upstream + prune the local origin tracking
    # ref. Mirrors the operator-walked-the-pipeline state that caused
    # issue #48 on foreman#46 (2026-06-02 dogfood).
    subprocess.run(
        ["git", "push", "origin", "--delete", "foreman/issue-42"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "prune", "origin"], cwd=clone, check=True, capture_output=True
    )

    # Second call: worktree exists → idempotent path → base_branch
    # recomputed from current origin state (now: spec branch gone, spec
    # doc on origin/main → fallback path).
    second = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)
    assert second.path == first.path, (
        "Idempotent re-call must return the same worktree path"
    )
    assert second.base_branch == "main", (
        "After spec branch deletion, idempotent re-call must RECOMPUTE "
        "base_branch from current origin state — should fall back to "
        f"'main'; got {second.base_branch!r}"
    )


def test_attach_impl_attaches_to_existing_impl_branch(tmp_path: Path) -> None:
    """``attach_impl`` is the read-side counterpart to ``create_impl`` —
    downstream roles (Reviewer on the impl PR, eventual impl-Fixer) attach
    a sibling worktree at ``impl-<N>/`` checked out on the impl branch the
    Worker already pushed. No new branch is created (no ``-b`` passed)."""
    clone = tmp_path / "clone"
    spec_head = _seed_clone_with_spec_branch_pushed(clone, ticket_id=42)

    # Seed an additional ``foreman/impl-42`` branch stacked on the spec
    # branch the helper just pushed. This mirrors the state of the clone
    # after the Worker's ``create_impl`` + push.
    impl_branch = "foreman/impl-42"
    subprocess.run(
        ["git", "checkout", "-b", impl_branch, "foreman/issue-42"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    (clone / "src").mkdir(parents=True, exist_ok=True)
    (clone / "src" / "foo.py").write_text("# impl stub\n")
    subprocess.run(["git", "add", "."], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: impl stub"], cwd=clone, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "origin", impl_branch], cwd=clone, check=True, capture_output=True
    )
    impl_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "main"], cwd=clone, check=True, capture_output=True)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = mgr.attach_impl(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert wt_path.exists()
    assert wt_path == worktrees_root / "voice" / "impl-42"

    branch_check = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch_check.stdout.strip() == impl_branch

    # The attached worktree's HEAD must match the existing impl branch
    # tip (proves attach to existing branch, not create-with-new-branch
    # off main / spec).
    attached_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert attached_head == impl_head
    assert attached_head != spec_head, (
        "attach_impl must check out the impl branch's tip, not the spec tip"
    )


# ----------------------------------------------------------------------
# dev_base_branch override — Foreman issue #16
#
# When the project's active dev line lives on a feature branch (e.g. during
# walking-skeleton phase), the Planner must branch new spec worktrees from
# ``origin/<feature-branch>`` instead of ``origin/<default>``. Concrete
# case: foreman itself — ``origin/main`` was scaffold-only while all real
# work was on ``feat/walking-skeleton``. ``WorktreeManager.create`` accepts
# a ``dev_base_branch`` kwarg; when set it overrides the default-branch
# resolution. When None (the default), behavior is unchanged from before
# the override existed.
# ----------------------------------------------------------------------


def _seed_clone_with_alt_branch_on_origin(
    clone: Path, *, origin_path: Path, alt_branch: str
) -> tuple[str, str]:
    """Init a clone whose ``main`` is the initial scaffold commit, plus
    an ``alt_branch`` on origin that carries an extra commit. Mirrors
    foreman's real walking-skeleton shape.

    Returns ``(origin_main_tip, origin_alt_tip)`` so callers can verify
    which tip the new spec branch is based on.
    """
    _init_git_repo(clone, origin_path=origin_path)
    origin_main_tip = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Create the alt branch with an extra commit and push it.
    subprocess.run(
        ["git", "checkout", "-b", alt_branch],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    (clone / "walking-skeleton-work.txt").write_text("active dev line\n")
    subprocess.run(
        ["git", "add", "walking-skeleton-work.txt"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "feat: walking skeleton"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", alt_branch],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    origin_alt_tip = subprocess.run(
        ["git", "rev-parse", f"origin/{alt_branch}"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # Return to main so subsequent operations don't accidentally depend on
    # the alt branch being checked out.
    subprocess.run(["git", "checkout", "main"], cwd=clone, check=True, capture_output=True)
    assert origin_main_tip != origin_alt_tip, "test setup: tips must differ"
    return origin_main_tip, origin_alt_tip


def test_create_with_dev_base_branch_none_uses_default_branch(tmp_path: Path) -> None:
    """When ``dev_base_branch=None`` (the default), ``create`` resolves
    the base via ``_resolve_default_branch`` exactly as before — same
    contract as every existing call site, guaranteeing the new kwarg is
    additive and doesn't shift behavior for projects that omit it."""
    clone = tmp_path / "clone"
    clone.mkdir()
    origin_main_tip, _ = _seed_clone_with_alt_branch_on_origin(
        clone,
        origin_path=tmp_path / "origin.git",
        alt_branch="feat/walking-skeleton",
    )

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = mgr.create(clone_path=clone, repo_slug="foreman", ticket_id=16, dev_base_branch=None)

    branch_tip = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch_tip == origin_main_tip, (
        "dev_base_branch=None must fall back to origin/<default>; "
        f"expected origin/main tip ({origin_main_tip}), got {branch_tip}"
    )


def test_create_with_dev_base_branch_uses_alt_branch(tmp_path: Path) -> None:
    """When ``dev_base_branch="feat/walking-skeleton"``, the new spec
    branch must be based on ``origin/feat/walking-skeleton`` — NOT
    ``origin/main``. This is the whole point of the knob: support
    projects mid-walking-skeleton where main is stale and the real
    development line is on a feature branch.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    origin_main_tip, origin_alt_tip = _seed_clone_with_alt_branch_on_origin(
        clone,
        origin_path=tmp_path / "origin.git",
        alt_branch="feat/walking-skeleton",
    )

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = mgr.create(
        clone_path=clone,
        repo_slug="foreman",
        ticket_id=16,
        dev_base_branch="feat/walking-skeleton",
    )

    branch_tip = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch_tip == origin_alt_tip, (
        "dev_base_branch override must base the new branch on the alt "
        f"branch's origin tip ({origin_alt_tip}); got {branch_tip}"
    )
    assert branch_tip != origin_main_tip, (
        "Sanity: branch tip must NOT equal origin/main — that would mean "
        "the override silently fell back to the default branch"
    )


def test_create_with_dev_base_branch_fetches_alt_branch_not_default(
    tmp_path: Path,
) -> None:
    """When ``dev_base_branch`` is set, the ``git fetch origin <branch>``
    that runs before ``worktree add`` must target the alt branch, not the
    default branch. Otherwise we'd refresh main and base on a stale alt
    origin ref — the same drift bug the fetch was added to prevent, just
    in the other direction.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _seed_clone_with_alt_branch_on_origin(
        clone,
        origin_path=tmp_path / "origin.git",
        alt_branch="feat/walking-skeleton",
    )

    real_run = subprocess.run
    fetched_branches: list[str] = []

    def recording_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(cmd, list) and len(cmd) >= 4 and cmd[0] == "git" and cmd[1] == "fetch":
            # cmd looks like ['git', 'fetch', '--quiet', 'origin', '<branch>']
            fetched_branches.append(cmd[-1])
        return real_run(cmd, *args, **kwargs)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)

    with patch("foreman.worktree.subprocess.run", side_effect=recording_run):
        mgr.create(
            clone_path=clone,
            repo_slug="foreman",
            ticket_id=16,
            dev_base_branch="feat/walking-skeleton",
        )

    assert "feat/walking-skeleton" in fetched_branches, (
        "Expected the alt branch to be fetched from origin before worktree "
        f"add; only fetched {fetched_branches!r}"
    )
    assert "main" not in fetched_branches, (
        "When dev_base_branch is set we must NOT fetch the default branch — "
        "that would suggest the override is being silently ignored or doubled"
    )


def test_create_with_dev_base_branch_continues_when_fetch_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If ``git fetch origin <dev_base_branch>`` fails (e.g. offline), the
    create must still proceed using the cached origin ref — matching the
    existing fetch-failure tolerance for the default-branch path. Forcing
    a hard failure here would block ticket execution on transient
    connectivity issues.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _, origin_alt_tip = _seed_clone_with_alt_branch_on_origin(
        clone,
        origin_path=tmp_path / "origin.git",
        alt_branch="feat/walking-skeleton",
    )

    # Break the origin URL so fetch fails — but the cached
    # ``refs/remotes/origin/feat/walking-skeleton`` ref still exists from
    # the seed push, so worktree add can resolve it.
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")],
        cwd=clone,
        check=True,
        capture_output=True,
    )

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt_path = mgr.create(
        clone_path=clone,
        repo_slug="foreman",
        ticket_id=16,
        dev_base_branch="feat/walking-skeleton",
    )

    assert wt_path.exists(), (
        "worktree must still be created when fetch fails with dev_base_branch set"
    )
    branch_tip = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch_tip == origin_alt_tip, (
        "When fetch fails, must fall back to the cached alt-branch ref"
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "git fetch" in combined and "warn" in combined.lower(), (
        "Expected a stderr warning about git fetch failure for the alt branch"
    )


# ----------------------------------------------------------------------
# Stage 3e follow-up — WorktreeManager threads ``role_token`` into every
# git/uv subprocess so commits + pushes attribute to the role bot's
# identity, not the daemon's parent ``GH_TOKEN``. Pass 2 adversarial
# review LOW: WorktreeManager's git fetch / worktree add / uv sync
# subprocess calls were inheriting the daemon's parent ``GH_TOKEN``
# (CI runner, dev shell, whichever role last set one), leaking the
# daemon's identity on private repos and risking auth failures on
# repos with role-specific permissions.
# ----------------------------------------------------------------------


def _capture_envs_for_create(
    captured_envs: list[dict[str, str] | None],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Recorder that captures the ``env`` kwarg for every git subprocess
    while delegating execution to real ``subprocess.run`` (so the
    underlying git operations still succeed and the worktree gets
    built). Skips ``uv sync`` because the fixture clones don't have
    a real pyproject installed, but the production code path that
    runs uv sync is exercised by other tests above.
    """
    real_run = subprocess.run

    def recording_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            captured_envs.append(kwargs.get("env"))
        return real_run(cmd, *args, **kwargs)

    return recording_run


def test_create_threads_role_token_into_git_subprocess_envs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every git subprocess inside ``create()`` receives
    ``GH_TOKEN=<role_token>`` when the manager is constructed with one.

    Without this threading, ``git fetch`` / ``git worktree add`` would
    inherit whatever ``GH_TOKEN`` the daemon's parent process happened
    to carry — leaking the daemon's identity (or worse, a previous
    role's) into the role bot's git ops. The role token must win,
    deterministically.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, origin_path=tmp_path / "origin.git")

    # Parent process carries an unrelated GH_TOKEN — the wrong-identity
    # token the daemon might have inherited. The role token must override it.
    monkeypatch.setenv("GH_TOKEN", "ghs_PARENT_DAEMON_TOKEN_SHOULD_NOT_WIN")

    captured_envs: list[dict[str, str] | None] = []
    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(
        worktrees_root=worktrees_root,
        role_token="ghs_role_planner_token_xyz",
    )

    with patch(
        "foreman.worktree.subprocess.run",
        side_effect=_capture_envs_for_create(captured_envs),
    ):
        mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert captured_envs, "expected at least one git subprocess call"
    for env in captured_envs:
        assert env is not None, (
            "every git call in create() must pass env=filtered_subprocess_env(role_token=...); "
            "default env=None would inherit the parent's GH_TOKEN"
        )
        assert env.get("GH_TOKEN") == "ghs_role_planner_token_xyz", (
            f"git subprocess inherited wrong GH_TOKEN={env.get('GH_TOKEN')!r}; "
            "the role token must override the parent process's GH_TOKEN"
        )


def test_create_without_role_token_preserves_parent_gh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the manager is constructed without ``role_token`` (manual
    CLI invocation, existing tests), the parent ``GH_TOKEN`` survives —
    we don't accidentally strip it. The role-token override is OPTIONAL
    by design: not every caller has a role identity in scope.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, origin_path=tmp_path / "origin.git")

    monkeypatch.setenv("GH_TOKEN", "ghs_parent_inherited_token")

    captured_envs: list[dict[str, str] | None] = []
    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)  # no role_token

    with patch(
        "foreman.worktree.subprocess.run",
        side_effect=_capture_envs_for_create(captured_envs),
    ):
        mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert captured_envs, "expected at least one git subprocess call"
    for env in captured_envs:
        assert env is not None
        # filtered_subprocess_env preserves the parent GH_TOKEN when no
        # role_token override is supplied.
        assert env.get("GH_TOKEN") == "ghs_parent_inherited_token"


def test_create_impl_threads_role_token_into_git_subprocess_envs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same anti-leak invariant for ``create_impl``: fetch + symbolic-ref +
    rev-parse + cat-file + worktree-add all must carry the worker's role
    token, not whatever the parent shell had set.
    """
    clone = tmp_path / "clone"
    _seed_clone_with_spec_branch_pushed(clone, ticket_id=42)

    monkeypatch.setenv("GH_TOKEN", "ghs_PARENT_DAEMON_TOKEN_SHOULD_NOT_WIN")

    captured_envs: list[dict[str, str] | None] = []
    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(
        worktrees_root=worktrees_root,
        role_token="ghs_role_worker_token_xyz",
    )

    with patch(
        "foreman.worktree.subprocess.run",
        side_effect=_capture_envs_for_create(captured_envs),
    ):
        mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert captured_envs, "expected at least one git subprocess call from create_impl"
    for env in captured_envs:
        assert env is not None
        assert env.get("GH_TOKEN") == "ghs_role_worker_token_xyz", (
            f"create_impl git subprocess inherited wrong GH_TOKEN={env.get('GH_TOKEN')!r}; "
            "the role token must override the parent process's GH_TOKEN"
        )


def test_attach_threads_role_token_into_git_subprocess_envs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``attach`` runs ``git fetch`` (when the branch isn't local) and
    ``git worktree add`` — both must carry the role token. Before the
    Stage 3e follow-up, ``attach`` didn't pass an ``env=`` at all, so
    every fetch leaked the parent's GH_TOKEN AND VIRTUAL_ENV.
    """
    clone = tmp_path / "clone"
    _seed_clone_with_spec_branch_pushed(clone, ticket_id=42)

    # Force the "branch not local" path so _local_branch_exists +
    # git fetch both fire. The spec branch was pushed to origin and
    # then we ``git checkout main`` in the fixture, but the local
    # ref ``refs/heads/foreman/issue-42`` still exists. Delete it
    # so ``attach`` hits the fetch path.
    subprocess.run(
        ["git", "branch", "-D", "foreman/issue-42"],
        cwd=clone,
        check=True,
        capture_output=True,
    )

    monkeypatch.setenv("GH_TOKEN", "ghs_PARENT_DAEMON_TOKEN_SHOULD_NOT_WIN")

    captured_envs: list[dict[str, str] | None] = []
    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(
        worktrees_root=worktrees_root,
        role_token="ghs_role_reviewer_token_xyz",
    )

    with patch(
        "foreman.worktree.subprocess.run",
        side_effect=_capture_envs_for_create(captured_envs),
    ):
        mgr.attach(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert captured_envs, "expected at least one git subprocess call from attach"
    for env in captured_envs:
        assert env is not None, (
            "every git call in attach() must pass env=filtered_subprocess_env(role_token=...); "
            "previously these calls passed no env at all and leaked the parent identity"
        )
        assert env.get("GH_TOKEN") == "ghs_role_reviewer_token_xyz", (
            f"attach git subprocess inherited wrong GH_TOKEN={env.get('GH_TOKEN')!r}; "
            "the reviewer's role token must win"
        )


def test_attach_impl_threads_role_token_into_git_subprocess_envs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same invariant for ``attach_impl`` — the downstream Reviewer /
    impl-Fixer attach worktree carries the role token.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, origin_path=tmp_path / "origin.git")

    # Seed an impl branch on origin so attach_impl's fetch finds it.
    impl_branch_name = "foreman/impl-42"
    subprocess.run(
        ["git", "checkout", "-b", impl_branch_name],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    (clone / "impl.txt").write_text("impl seed\n")
    subprocess.run(["git", "add", "impl.txt"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "impl seed"], cwd=clone, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "origin", impl_branch_name],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=clone, check=True, capture_output=True)
    # Delete the local branch to force the fetch path.
    subprocess.run(
        ["git", "branch", "-D", impl_branch_name], cwd=clone, check=True, capture_output=True
    )

    monkeypatch.setenv("GH_TOKEN", "ghs_PARENT_DAEMON_TOKEN_SHOULD_NOT_WIN")

    captured_envs: list[dict[str, str] | None] = []
    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(
        worktrees_root=worktrees_root,
        role_token="ghs_role_reviewer_impl_token_xyz",
    )

    with patch(
        "foreman.worktree.subprocess.run",
        side_effect=_capture_envs_for_create(captured_envs),
    ):
        mgr.attach_impl(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert captured_envs, "expected at least one git subprocess call from attach_impl"
    for env in captured_envs:
        assert env is not None
        assert env.get("GH_TOKEN") == "ghs_role_reviewer_impl_token_xyz"


def test_cleanup_threads_role_token_into_git_subprocess_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cleanup`` runs ``git worktree remove`` — a local-only op, but
    still routed through the env filter for consistency (and to avoid
    leaking VIRTUAL_ENV into any worktree-removal hook the target repo
    happens to run).
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, origin_path=tmp_path / "origin.git")

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(
        worktrees_root=worktrees_root,
        role_token="ghs_role_cleanup_token_xyz",
    )
    wt_path = mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    monkeypatch.setenv("GH_TOKEN", "ghs_PARENT_DAEMON_TOKEN_SHOULD_NOT_WIN")

    captured_envs: list[dict[str, str] | None] = []
    with patch(
        "foreman.worktree.subprocess.run",
        side_effect=_capture_envs_for_create(captured_envs),
    ):
        mgr.cleanup(clone_path=clone, worktree_path=wt_path)

    assert captured_envs, "expected git worktree remove subprocess call"
    for env in captured_envs:
        assert env is not None
        assert env.get("GH_TOKEN") == "ghs_role_cleanup_token_xyz"


def test_ensure_clone_creates_clone_when_missing(tmp_path: Path) -> None:
    """When the configured local_clone_path doesn't exist, ensure_clone
    must `git clone <repo_url>` into it. Used by the container's first
    boot when the foreman-repos volume is empty.

    We stub the actual clone with a local bare repo so the test doesn't
    hit the network."""
    # Set up a local "origin" the daemon will clone from
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=seed, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "seed@example.com"],
        cwd=seed, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Seed"],
        cwd=seed, check=True, capture_output=True,
    )
    (seed / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "clone", "--bare", str(seed), str(origin)], check=True, capture_output=True)

    target = tmp_path / "repos" / "myproject"
    assert not target.exists(), "precondition: target must be missing"

    from foreman.worktree import ensure_clone
    ensure_clone(repo_url=str(origin), clone_path=target)

    assert target.exists(), "clone directory should exist after ensure_clone"
    assert (target / ".git").exists(), "should be a real git clone, not just a dir"
    # Should NOT re-clone on second call (idempotent)
    first_mtime = (target / ".git").stat().st_mtime
    ensure_clone(repo_url=str(origin), clone_path=target)
    second_mtime = (target / ".git").stat().st_mtime
    assert first_mtime == second_mtime, "ensure_clone must be idempotent on second call"


# ----------------------------------------------------------------------
# foreman#291 — fetch_origin_default_branch + per-dispatch defense in depth
# ----------------------------------------------------------------------


def test_fetch_origin_default_branch_refreshes_origin_default(tmp_path: Path) -> None:
    """``fetch_origin_default_branch`` resolves the default branch name
    via ``origin/HEAD`` and calls ``_fetch_origin_branch`` against it.
    Land a fresh commit on origin (bypassing the client clone) and
    confirm that calling the helper updates the local
    ``refs/remotes/origin/main`` ref to the new tip.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    origin_path = tmp_path / "origin.git"
    _init_git_repo(clone, origin_path=origin_path)

    # Pre-fetch baseline.
    baseline_origin_tip = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Land a fresh commit on origin via a side checkout so the client
    # clone's ``refs/remotes/origin/main`` ref is stale.
    side = tmp_path / "side"
    subprocess.run(
        ["git", "clone", str(origin_path), str(side)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "side@example.com"],
        cwd=side,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Side"],
        cwd=side,
        check=True,
        capture_output=True,
    )
    (side / "new.txt").write_text("upstream advanced\n")
    subprocess.run(
        ["git", "add", "new.txt"], cwd=side, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "upstream advanced"],
        cwd=side,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "main"], cwd=side, check=True, capture_output=True
    )

    # Confirm precondition: client clone is still on the old tip.
    cached_origin_tip = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert cached_origin_tip == baseline_origin_tip, (
        "test setup: client clone's cached origin/main must NOT have moved "
        "without a fetch"
    )

    # The fix: fetch_origin_default_branch resolves the default branch
    # name and updates the cached origin ref.
    fetch_origin_default_branch(clone)

    refreshed_origin_tip = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=clone,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert refreshed_origin_tip != baseline_origin_tip, (
        "After fetch_origin_default_branch, client's origin/main must "
        "track the new upstream tip"
    )


def test_create_still_fetches_base_branch_per_dispatch(tmp_path: Path) -> None:
    """foreman#291 defense in depth: the per-dispatch fetch in
    ``WorktreeManager.create`` must still fire regardless of whether
    the per-poll ``OnPollFetch`` strategy is selected at the
    reconciler scope. Removing one of the two would re-open the drift
    window the issue exists to close.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    _init_git_repo(clone, origin_path=tmp_path / "origin.git")

    real_run = subprocess.run
    fetched_branches: list[str] = []

    def recording_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(cmd, list) and len(cmd) >= 4 and cmd[0] == "git" and cmd[1] == "fetch":
            # cmd looks like ['git', 'fetch', '--quiet', '--prune', 'origin', '<branch>']
            fetched_branches.append(cmd[-1])
        return real_run(cmd, *args, **kwargs)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)

    with patch("foreman.worktree.subprocess.run", side_effect=recording_run):
        mgr.create(clone_path=clone, repo_slug="voice", ticket_id=42)

    assert "main" in fetched_branches, (
        "WorktreeManager.create must still fetch origin/main per dispatch "
        "even when a per-poll refresh strategy runs at reconciler scope; "
        f"observed fetches: {fetched_branches!r}"
    )
