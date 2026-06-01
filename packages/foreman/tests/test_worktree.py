"""Tests for per-ticket git worktree create + cleanup."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from foreman.worktree import WorktreeManager


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
        (path / "pyproject.toml").write_text(
            "[project]\nname = 'fake'\nversion = '0.0.0'\n"
        )
        subprocess.run(
            ["git", "add", "pyproject.toml"], cwd=path, check=True, capture_output=True
        )
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=path, check=True, capture_output=True
    )
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
    assert "cmd" not in capture, (
        "uv sync must not run when uv is not on PATH"
    )


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


def test_create_filters_env_for_uv_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    subprocess.run(
        ["git", "init", "-b", "master"], cwd=clone, check=True, capture_output=True
    )
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
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=clone, check=True, capture_output=True
    )
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
    subprocess.run(
        ["git", "push", "origin", "master"], cwd=clone, check=True, capture_output=True
    )
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
        "Default branch must be resolved from origin/HEAD (master here), "
        "not hardcoded as main."
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

    def recording_run(
        cmd: Any, *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
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
        assert "VIRTUAL_ENV" not in env, (
            "VIRTUAL_ENV leaked into a git subprocess inside create()"
        )


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
    (spec_dir / f"foreman-issue-{ticket_id}-spec.md").write_text(
        f"# Spec for issue #{ticket_id}\n"
    )
    subprocess.run(["git", "add", "."], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "spec doc"], cwd=clone, check=True, capture_output=True
    )
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
    wt_path = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)

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
        f"impl-<N> branch must be based on spec branch tip ({spec_head}); "
        f"got {impl_tip}"
    )


def test_create_impl_is_idempotent_on_existing_path(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    _seed_clone_with_spec_branch_pushed(clone, ticket_id=42)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    wt1 = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)
    wt2 = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)
    assert wt1 == wt2


def test_create_impl_separate_from_spec_worktree(tmp_path: Path) -> None:
    """``impl-<N>`` and ``issue-<N>`` are sibling worktrees, NEVER the same
    dir — the Worker must not inherit a Fixer's WIP spec edits."""
    clone = tmp_path / "clone"
    _seed_clone_with_spec_branch_pushed(clone, ticket_id=42)

    worktrees_root = tmp_path / "worktrees"
    mgr = WorktreeManager(worktrees_root=worktrees_root)
    impl_wt = mgr.create_impl(clone_path=clone, repo_slug="voice", ticket_id=42)
    spec_wt = mgr.attach(clone_path=clone, repo_slug="voice", ticket_id=42)
    assert impl_wt != spec_wt
    assert impl_wt.name == "impl-42"
    assert spec_wt.name == "issue-42"


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

    def recording_run(
        cmd: Any, *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
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
