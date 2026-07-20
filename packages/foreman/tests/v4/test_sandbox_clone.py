"""Unit tests for the daemon-side sandbox clone-prep helper (foreman#556)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foreman.v4.sandbox_clone import (
    cleanup_ticket_scratch,
    prepare_sandbox_clone,
    sandbox_clone_argv,
    tokenized_origin_url,
)


def _init_repo_with_commit(repo_path: Path) -> None:
    """Init a tiny real git repo at ``repo_path`` with one commit.

    Shared setup for the real-git (non-fake-runner) tests below — these
    exercise the actual ``subprocess.run`` default runner, not the fake
    argv-recording seam, per foreman#556 Task 3's requirement that the
    clone-prep path is proven against real git, not just argv shape.
    """
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
    (repo_path / "f.txt").write_text("hi", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-q", "-m", "init"], check=True)


def test_clone_argv_is_local_clone() -> None:
    # Brief-snippet fix: hardcoded POSIX literals broke on Windows dev
    # boxes, where str(Path("/foreman/...")) renders with backslashes.
    # sandbox_clone_argv uses plain str() (matching worktree.ensure_clone's
    # convention and test_prepare_clones_when_absent_then_repoints_origin
    # below), so derive expected strings from the same Path objects.
    base = Path("/foreman/repos/foreman")
    dest = Path("/scratch/clone")
    argv = sandbox_clone_argv(base, dest)
    assert argv == ["git", "clone", "--local", str(base), str(dest)]


def test_tokenized_origin_url_embeds_token_for_https() -> None:
    url = tokenized_origin_url("https://github.com/o/n.git", "ghs_TOK")
    assert url == "https://x-access-token:ghs_TOK@github.com/o/n.git"


def test_tokenized_origin_url_passes_non_https_through() -> None:
    assert tokenized_origin_url("git@github.com:o/n.git", "ghs_TOK") == "git@github.com:o/n.git"


def test_prepare_clones_when_absent_then_repoints_origin(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    dest = tmp_path / "job" / "clone"
    prepare_sandbox_clone(
        base_clone_path=tmp_path / "base",
        dest_clone_path=dest,
        repo_url="https://github.com/o/n.git",
        role_token="ghs_TOK",
        runner=fake_runner,
    )
    assert calls[0] == ["git", "clone", "--local", str(tmp_path / "base"), str(dest)]
    assert calls[1] == [
        "git",
        "-C",
        str(dest),
        "remote",
        "set-url",
        "origin",
        "https://x-access-token:ghs_TOK@github.com/o/n.git",
    ]
    assert dest.parent.exists()  # parent created


def test_prepare_skips_clone_when_present_but_always_refreshes_origin(tmp_path: Path) -> None:
    """Idempotent re-entry (retry): reuse the existing clone but refresh the (short-lived) token."""
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    dest = tmp_path / "clone"
    (dest / ".git").mkdir(parents=True)  # simulate an existing clone
    prepare_sandbox_clone(
        base_clone_path=tmp_path / "base",
        dest_clone_path=dest,
        repo_url="https://github.com/o/n.git",
        role_token="ghs_NEW",
        runner=fake_runner,
    )
    assert not any(c[:3] == ["git", "clone", "--local"] for c in calls)
    assert calls[-1][-1] == "https://x-access-token:ghs_NEW@github.com/o/n.git"


def test_prepare_sandbox_clone_real_git_creates_hardlinked_clone_and_repoints_origin(
    tmp_path: Path,
) -> None:
    """No fake runner: exercises the real ``subprocess.run`` default path.

    Proves the daemon-side clone-prep against a real temp git repo (no
    bwrap/userns needed — this runs on the plain host, not in a sandbox
    box): the clone lands on disk, its object store is hardlinked to the
    base (co-located under the same ``tmp_path`` filesystem, mirroring
    ``scratch_root``'s repos-volume co-location in production), and
    ``origin`` is re-pointed at the tokenized GitHub URL.
    """
    base = tmp_path / "base"
    _init_repo_with_commit(base)

    dest = tmp_path / "job" / "clone"
    prepare_sandbox_clone(
        base_clone_path=base,
        dest_clone_path=dest,
        repo_url="https://github.com/o/n.git",
        role_token="ghs_REAL",
    )

    assert (dest / ".git").exists()

    origin = subprocess.run(
        ["git", "-C", str(dest), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert origin == "https://x-access-token:ghs_REAL@github.com/o/n.git"

    # Hardlink proof: base and dest loose objects share the same
    # (device, inode) — git's --local clone hardlinked them instead of
    # copying, confirming the free-clone contract sandbox_clone_argv's
    # docstring promises when co-located.
    base_objects = sorted((base / ".git" / "objects").glob("*/*"))
    assert base_objects, "expected at least one loose object in the base repo"
    for obj in base_objects:
        dest_obj = dest / ".git" / "objects" / obj.parent.name / obj.name
        assert dest_obj.exists()
        base_stat, dest_stat = obj.stat(), dest_obj.stat()
        assert (base_stat.st_dev, base_stat.st_ino) == (dest_stat.st_dev, dest_stat.st_ino), (
            f"{obj} and {dest_obj} are not hardlinked (different inode) — "
            f"git --local silently fell back to a copy, likely because "
            f"the two paths are not co-located on the same filesystem"
        )


def test_prepare_sandbox_clone_real_git_idempotent_reentry_refreshes_origin(
    tmp_path: Path,
) -> None:
    """No fake runner: a retry against an already-cloned dest skips the
    clone step but still re-points origin with the rotated token."""
    base = tmp_path / "base"
    _init_repo_with_commit(base)

    dest = tmp_path / "clone"
    prepare_sandbox_clone(
        base_clone_path=base,
        dest_clone_path=dest,
        repo_url="https://github.com/o/n.git",
        role_token="ghs_OLD",
    )
    prepare_sandbox_clone(
        base_clone_path=base,
        dest_clone_path=dest,
        repo_url="https://github.com/o/n.git",
        role_token="ghs_NEW",
    )

    origin = subprocess.run(
        ["git", "-C", str(dest), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert origin == "https://x-access-token:ghs_NEW@github.com/o/n.git"


def test_cleanup_ticket_scratch_removes_role_dirs(tmp_path: Path) -> None:
    scratch_root = tmp_path / "scratch"
    for base_name in ("planner", "reviewer", "fixer", "worker"):
        (scratch_root / "proj" / f"{base_name}-42" / "clone").mkdir(parents=True)

    removed = cleanup_ticket_scratch(scratch_root=scratch_root, project="proj", issue_number=42)

    assert len(removed) == 4
    for base_name in ("planner", "reviewer", "fixer", "worker"):
        assert not (scratch_root / "proj" / f"{base_name}-42").exists()


def test_cleanup_ticket_scratch_is_idempotent_on_missing_dirs(tmp_path: Path) -> None:
    scratch_root = tmp_path / "scratch"
    removed = cleanup_ticket_scratch(scratch_root=scratch_root, project="proj", issue_number=99)
    assert removed == []


def test_cleanup_removes_all_role_job_dirs_for_ticket(tmp_path: Path) -> None:
    """A different ticket's scratch dir under the same project must survive."""
    root = tmp_path / ".scratch"
    for base in ("planner", "reviewer", "worker"):
        (root / "foreman" / f"{base}-42" / "clone").mkdir(parents=True)
    # a different ticket's dir must survive
    (root / "foreman" / "worker-7" / "clone").mkdir(parents=True)

    removed = cleanup_ticket_scratch(scratch_root=root, project="foreman", issue_number=42)

    assert not (root / "foreman" / "planner-42").exists()
    assert not (root / "foreman" / "worker-42").exists()
    assert (root / "foreman" / "worker-7").exists()  # untouched
    assert len(removed) == 3


@pytest.mark.xfail(
    reason="fixed in the next commit: prepare_sandbox_clone blanket fetch", strict=True
)
def test_repro_406_box_from_stale_base_lacks_pushed_commit(tmp_path: Path) -> None:
    """REPRO of #406: prepare_sandbox_clone clones from the base and re-points
    origin, but does NOT fetch — so a commit pushed to origin after the base
    was cloned is absent in the box, and `git diff origin/main...<sha>` fails
    (exit 128). This test FAILS today and passes once Task 2 adds the fetch."""
    import subprocess

    from foreman.v4 import sandbox_clone

    def git(*args: str, cwd: Path) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    # stand-in origin (bare) with a main branch + one commit
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"], check=True)
    (seed / "a.txt").write_text("a\n")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "init", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)

    # daemon base clone (co-located so --local hardlinks) — stale snapshot
    base = tmp_path / "base"
    subprocess.run(["git", "clone", str(origin), str(base)], check=True)

    # a prior role pushes a NEW commit to origin AFTER the base was cloned
    (seed / "b.txt").write_text("b\n")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "role-A change", cwd=seed)
    new_sha = git("rev-parse", "HEAD", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)

    # next role's box, via the current prepare flow (origin re-pointed at the
    # real origin path here since there's no token auth in the test)
    dest = tmp_path / "scratch" / "box"
    sandbox_clone.prepare_sandbox_clone(
        base_clone_path=base,
        dest_clone_path=dest,
        repo_url=str(origin),
        role_token="ghs_UNUSED",
        runner=lambda argv: subprocess.run(argv, check=True, capture_output=True, text=True),
    )
    # BUG: the box lacks new_sha, so a diff against it fails (git exit 128)
    result = subprocess.run(
        ["git", "-C", str(dest), "diff", f"origin/main...{new_sha}"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"box is stale: diff against pushed commit failed (rc={result.returncode}): "
        f"{result.stderr.strip()}"
    )
