"""Per-ticket git worktree manager.

Each Foreman pipeline gets its own worktree at:
  <worktrees_root>/<repo_slug>/issue-<N>/

Branched as `foreman/issue-<N>`. All node commits for that ticket land on
that branch in that worktree. Cleanup happens at pipeline completion (or
deferred for failed pipelines per the spec).

Issue #10 follow-up — venv pre-population
-----------------------------------------
A target repo's pre-push hook (e.g. voice's ``.githooks/pre-push`` running
``just check``) executes ``uv run --no-sync mypy ...`` inside the per-ticket
worktree. Worktrees share git history with the clone but NOT gitignored
files like ``.venv``, so a freshly-created worktree has no venv of its own.
``--no-sync`` then makes uv create a fresh *empty* ``.venv`` and mypy
fails because no deps are installed.

Fix: after ``git worktree add`` succeeds, if the worktree has a
``pyproject.toml`` at root and ``uv`` is on PATH, run
``uv sync --all-packages`` in the worktree to pre-populate ``.venv``.
We pay this ~30 s cost once per pipeline (not per role-run), and the
Reviewer / Worker nodes that share the worktree both inherit the venv.

The sync uses the same env-var filter as
:class:`~foreman.git_hosts.github.GitHubProvider._git` — see
:mod:`foreman._env_filter` — so we don't re-leak ``VIRTUAL_ENV`` into the
foreign worktree's tools.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from foreman._env_filter import filtered_subprocess_env


class WorktreeManager:
    """Create + cleanup per-ticket git worktrees."""

    def __init__(self, worktrees_root: Path) -> None:
        self.worktrees_root = worktrees_root

    def create(self, clone_path: Path, repo_slug: str, ticket_id: int) -> Path:
        """Create a worktree for one ticket. Idempotent on existing worktree.

        After the worktree is created, attempt to pre-populate its ``.venv``
        via ``uv sync --all-packages`` so the target repo's pre-push hook
        finds installed deps when it runs ``uv run --no-sync`` later. The
        sync is best-effort: failures print a warning but do not abort
        worktree creation (the push step will then fail loudly with a
        clearer hook error, which is a better signal to the operator
        than masking the real problem here).
        """
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
        _maybe_sync_worktree_deps(wt_path)
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


def _maybe_sync_worktree_deps(worktree_path: Path) -> None:
    """Best-effort ``uv sync --all-packages`` for the worktree's venv.

    Skips silently if the worktree has no ``pyproject.toml`` at root or
    ``uv`` is not on PATH. On sync failure, prints a warning (with the
    uv stderr) but does not raise — the eventual ``git push`` will
    surface the missing-deps error from the hook itself, which is more
    actionable than a worktree-create exception.

    Env vars that would mis-direct uv toward Foreman's own venv
    (``VIRTUAL_ENV``, ``UV_PROJECT_ENVIRONMENT``, ...) are stripped via
    :func:`foreman._env_filter.filtered_subprocess_env` — the same filter
    used by :class:`~foreman.git_hosts.github.GitHubProvider._git`.
    """
    if not (worktree_path / "pyproject.toml").exists():
        return
    if shutil.which("uv") is None:
        return

    result = subprocess.run(
        ["uv", "sync", "--all-packages"],
        cwd=worktree_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(),
    )
    if result.returncode == 0:
        summary = _summarize_uv_sync(result.stdout, result.stderr)
        print(f"[foreman.worktree] uv sync: {summary}")
    else:
        # Print to stderr so it stands out, but do NOT raise. The push
        # step will surface a clearer hook error in a moment.
        print(
            f"[foreman.worktree] warning: uv sync failed in {worktree_path} "
            f"(rc={result.returncode}); the pre-push hook will likely fail next. "
            f"stderr:\n{result.stderr.strip()}",
            file=sys.stderr,
        )


def _summarize_uv_sync(stdout: str, stderr: str) -> str:
    """Pull a one-liner out of ``uv sync`` output.

    ``uv`` writes the human-readable status (``Resolved N packages``,
    ``Installed N packages``) to stderr in some versions and stdout in
    others, so check both. Falls back to a generic ``"completed"`` if
    no recognizable line is found.
    """
    for stream in (stderr, stdout):
        for line in stream.splitlines():
            line = line.strip()
            if line.startswith(("Installed ", "Resolved ", "Audited ")):
                return line
    return "completed"
