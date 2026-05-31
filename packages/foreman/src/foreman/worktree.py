"""Per-ticket git worktree manager.

Each Foreman pipeline gets its own worktree at:
  <worktrees_root>/<repo_slug>/issue-<N>/

Branched as `foreman/issue-<N>`. All node commits for that ticket land on
that branch in that worktree. Cleanup happens at pipeline completion (or
deferred for failed pipelines per the spec).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeManager:
    """Create + cleanup per-ticket git worktrees."""

    def __init__(self, worktrees_root: Path) -> None:
        self.worktrees_root = worktrees_root

    def create(self, clone_path: Path, repo_slug: str, ticket_id: int) -> Path:
        """Create a worktree for one ticket. Idempotent on existing worktree."""
        wt_path = self.worktrees_root / repo_slug / f"issue-{ticket_id}"
        if wt_path.exists():
            return wt_path
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        branch = f"foreman/issue-{ticket_id}"
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(wt_path)],
            cwd=clone_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return wt_path

    def cleanup(self, clone_path: Path, worktree_path: Path) -> None:
        """Remove a worktree. Safe to call on already-removed worktrees."""
        if not worktree_path.exists():
            return
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=clone_path,
            check=True,
            capture_output=True,
            text=True,
        )
