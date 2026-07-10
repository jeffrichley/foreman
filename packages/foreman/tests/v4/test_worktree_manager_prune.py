"""WorktreeManager.prune — operator lever for foreman reset.

Removes both ~/.foreman/worktrees/<project>/issue-N/ and impl-N/ via
``git worktree remove --force`` first, falling back to ``shutil.rmtree``
if that fails (e.g. the dir exists but isn't a registered worktree).
"""

from __future__ import annotations

from pathlib import Path

from foreman.worktree import WorktreeManager


def _make_worktree_root(tmp_path: Path, project: str, issue: int) -> Path:
    root = tmp_path / "worktrees"
    (root / project / f"issue-{issue}").mkdir(parents=True)
    (root / project / f"impl-{issue}").mkdir(parents=True)
    return root


def test_prune_removes_both_issue_and_impl_dirs(tmp_path: Path):
    root = _make_worktree_root(tmp_path, "agent_core", 180)
    clone_path = tmp_path / "clone"
    clone_path.mkdir()
    wt = WorktreeManager(worktrees_root=root)
    removed = wt.prune(
        project="agent_core",
        issue_number=180,
        clone_path=clone_path,
    )
    assert sorted(p.name for p in removed) == ["impl-180", "issue-180"]
    assert not (root / "agent_core" / "issue-180").exists()
    assert not (root / "agent_core" / "impl-180").exists()


def test_prune_missing_dirs_returns_empty_list(tmp_path: Path):
    root = tmp_path / "worktrees"
    root.mkdir()
    clone_path = tmp_path / "clone"
    clone_path.mkdir()
    wt = WorktreeManager(worktrees_root=root)
    removed = wt.prune(
        project="agent_core",
        issue_number=999,
        clone_path=clone_path,
    )
    assert removed == []


def test_prune_only_issue_present(tmp_path: Path):
    root = tmp_path / "worktrees"
    (root / "agent_core" / "issue-180").mkdir(parents=True)
    # impl-180 deliberately absent.
    clone_path = tmp_path / "clone"
    clone_path.mkdir()
    wt = WorktreeManager(worktrees_root=root)
    removed = wt.prune(
        project="agent_core",
        issue_number=180,
        clone_path=clone_path,
    )
    assert [p.name for p in removed] == ["issue-180"]


def test_prune_only_impl_present(tmp_path: Path):
    root = tmp_path / "worktrees"
    (root / "agent_core" / "impl-180").mkdir(parents=True)
    clone_path = tmp_path / "clone"
    clone_path.mkdir()
    wt = WorktreeManager(worktrees_root=root)
    removed = wt.prune(
        project="agent_core",
        issue_number=180,
        clone_path=clone_path,
    )
    assert [p.name for p in removed] == ["impl-180"]
