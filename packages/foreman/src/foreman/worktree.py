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

        The new ``foreman/issue-<N>`` branch is based on
        ``origin/<default-branch>``, NOT the clone's local HEAD. Branching
        off local HEAD inherits any drift between the clone's local default
        branch and ``origin`` — e.g., unpushed local commits or local commits
        that already shipped under a different SHA via squash-merge — and
        every spec PR then carries that drift as "extra commits" unrelated
        to the spec.

        Concrete example: voice's local ``main`` carried ``0200159``
        (release-pipeline work that had already shipped via PR #7 as a
        different SHA ``e64df33``). Every Foreman spec PR (#11, #15, #16,
        #17) inherited that drift. Basing on ``origin/<default-branch>``
        instead pins the new branch at the freshly-fetched origin tip.

        Before creating the branch we ``git fetch origin <default-branch>``
        so the local origin ref is current. The fetch is best-effort: a
        network failure prints a warning but does not abort — the worktree
        create may still succeed from local origin state, and forcing a
        hard failure here would block ticket execution on transient
        connectivity issues.

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
        default_branch = _resolve_default_branch(clone_path)
        _fetch_origin_branch(clone_path, default_branch)
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                branch,
                str(wt_path),
                f"origin/{default_branch}",
            ],
            cwd=clone_path,
            check=True,
            capture_output=True,
            text=True,
            env=filtered_subprocess_env(),
        )
        _maybe_sync_worktree_deps(wt_path)
        return wt_path

    def create_impl(self, clone_path: Path, repo_slug: str, ticket_id: int) -> Path:
        """Create a fresh worktree for the Worker's impl branch, stacked on
        the spec branch.

        Path: ``<worktrees_root>/<repo_slug>/impl-<N>/`` — deliberately a
        sibling of ``issue-<N>/`` (the spec-side worktree the Planner /
        Reviewer / Fixer share). Keeping the two worktrees separate lets
        the Worker's branch evolve independently of any in-flight Fixer
        edits to the spec doc — and lets a future ticket's Worker still
        find a clean tree even if a Fixer left the spec worktree mid-edit.
        Sharing one worktree would force the Worker to git stash / reset
        every time it inherited a Fixer's WIP state.

        Branch: ``foreman/impl-<N>``. Base: ``foreman/issue-<N>`` (the
        spec branch). Stacking is required: the impl PR is opened with
        base=spec-branch (D1 in the brief) so the spec PR is reviewable
        and mergeable independently. A future ticket retargets the impl
        PR's base to the repo default when the spec PR merges (out of
        scope here).

        Idempotent: if ``impl-<N>/`` already exists we return it
        untouched. Same fetch + uv-sync best-effort discipline as
        :meth:`create`. The fetch targets ``foreman/issue-<N>`` so the
        local origin ref is current — without this the worktree would
        attach to a stale origin ref and the Worker would build atop a
        spec older than the Planner's last push.
        """
        wt_path = self.worktrees_root / repo_slug / f"impl-{ticket_id}"
        if wt_path.exists():
            return wt_path
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        impl_branch = f"foreman/impl-{ticket_id}"
        spec_branch = f"foreman/issue-{ticket_id}"
        # Refresh the spec branch from origin so we stack on the Planner's
        # latest push, not a stale local ref. Best-effort — same rationale
        # as ``create``'s default-branch fetch.
        _fetch_origin_branch(clone_path, spec_branch)
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                impl_branch,
                str(wt_path),
                f"origin/{spec_branch}",
            ],
            cwd=clone_path,
            check=True,
            capture_output=True,
            text=True,
            env=filtered_subprocess_env(),
        )
        _maybe_sync_worktree_deps(wt_path)
        return wt_path

    def attach(self, clone_path: Path, repo_slug: str, ticket_id: int) -> Path:
        """Attach a worktree to an existing ``foreman/issue-<N>`` branch.

        Used by downstream roles (Reviewer / Fixer / Worker) that should NOT
        create a new branch — the Planner already opened ``foreman/issue-N``
        and pushed it. Idempotent: if the worktree path already exists, it
        is returned untouched.

        Falls back to a tracking-branch fetch if the local branch does not
        yet exist (the Reviewer may run on a clone where the branch only
        lives on the remote).

        Like :meth:`create`, this best-effort syncs the worktree's ``.venv``
        afterward so the target repo's pre-push hook can ``uv run --no-sync``
        without exploding.
        """
        wt_path = self.worktrees_root / repo_slug / f"issue-{ticket_id}"
        if wt_path.exists():
            return wt_path
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        branch = f"foreman/issue-{ticket_id}"
        if not _local_branch_exists(clone_path, branch):
            # Branch isn't local yet — fetch the remote ref so worktree add
            # can resolve it. The Planner pushes the branch, so origin should
            # have it. We tolerate fetch failure here and let the worktree
            # add command surface a clearer error.
            subprocess.run(
                ["git", "fetch", "origin", branch],
                cwd=clone_path,
                check=False,
                capture_output=True,
                text=True,
            )
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
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


def _resolve_default_branch(clone_path: Path) -> str:
    """Return the clone's default branch name (e.g. ``"main"``, ``"master"``).

    Resolves via ``git symbolic-ref --short refs/remotes/origin/HEAD``, which
    returns e.g. ``origin/main``; we strip the ``origin/`` prefix. ``git
    clone`` sets this symbolic ref automatically, so in normal use it will
    be present.

    If ``origin/HEAD`` is missing or the command fails for any reason, we
    fall back to ``"main"`` rather than raising — better to base the new
    branch on a likely-correct guess and let a downstream git command
    produce a clear "unknown revision" error if even that's wrong, than
    crash worktree creation in a less actionable spot.
    """
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=clone_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(),
    )
    if result.returncode != 0:
        return "main"
    ref = result.stdout.strip()
    prefix = "origin/"
    if ref.startswith(prefix):
        ref = ref[len(prefix) :]
    return ref or "main"


def _fetch_origin_branch(clone_path: Path, branch: str) -> None:
    """Best-effort ``git fetch origin <branch>`` to refresh the local origin ref.

    Without this, ``origin/<branch>`` may be stale and basing the new spec
    branch on it would re-introduce drift from the other direction (origin
    moved forward; local origin ref didn't). On network failure we print a
    warning to stderr and continue — the subsequent worktree-add may still
    succeed from the cached origin ref, and a hard failure here would block
    ticket execution on transient connectivity issues.

    Uses the same env filter as the worktree-add and ``uv sync`` calls so
    we don't leak ``VIRTUAL_ENV`` etc. into git hook invocations.
    """
    result = subprocess.run(
        ["git", "fetch", "--quiet", "origin", branch],
        cwd=clone_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(),
    )
    if result.returncode != 0:
        print(
            f"[foreman.worktree] warning: git fetch origin {branch} failed in "
            f"{clone_path} (rc={result.returncode}); proceeding with cached "
            f"origin ref. stderr:\n{result.stderr.strip()}",
            file=sys.stderr,
        )


def _local_branch_exists(clone_path: Path, branch: str) -> bool:
    """Return True if ``branch`` exists as a local ref in ``clone_path``."""
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=clone_path,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


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
