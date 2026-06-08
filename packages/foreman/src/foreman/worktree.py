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
from dataclasses import dataclass
from pathlib import Path

from foreman._env_filter import filtered_subprocess_env
from foreman.branches import impl_branch, spec_branch


def ensure_clone(*, repo_url: str, clone_path: Path) -> None:
    """Ensure ``clone_path`` is a valid git clone of ``repo_url``.

    First-run helper for the container: when the ``foreman-repos`` Docker
    volume is empty, ``clone_path`` doesn't exist yet, and the daemon
    must clone the project from origin before any worktree-add can
    happen. Idempotent: if ``clone_path`` already contains a ``.git``
    directory, this is a no-op so existing host-side clones aren't
    re-fetched on every daemon restart.

    Args:
        repo_url: Remote URL (HTTPS or SSH). Authentication via
            PATH-resolved credentials / ssh agent / app-token URL
            rewriting as per the caller's existing convention.
        clone_path: Local filesystem path where the clone should live.

    Raises:
        subprocess.CalledProcessError: if ``git clone`` fails.
    """
    if (clone_path / ".git").exists():
        return
    clone_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", repo_url, str(clone_path)],
        check=True,
        capture_output=True,
    )


@dataclass(frozen=True)
class ImplWorktreeResult:
    """Outcome of :meth:`WorktreeManager.create_impl`.

    Carries both the worktree filesystem path and the branch the impl
    PR should target as its base. The latter encodes the decision
    ``create_impl`` made between two paths:

    - **Stacked path** (D1 in the build brief): ``origin/foreman/issue-<N>``
      exists, so the impl worktree is branched off it and the impl PR
      stacks on the spec PR. ``base_branch == "foreman/issue-<N>"``.
    - **Fallback path** (issue #48): ``origin/foreman/issue-<N>`` is gone
      (spec PR was merged with ``--delete-branch``, the repo has
      "automatically delete head branches" on, or an operator deleted
      it manually), but the spec doc demonstrably landed on the
      default branch. The impl worktree is branched off
      ``origin/<default>`` and the impl PR opens with
      ``base=<default>`` from the start — no stacking, no retarget
      step needed. ``base_branch == <default-branch-name>``.

    On idempotent re-call (the impl worktree path already exists),
    ``base_branch`` is recomputed from current origin state — the
    same probe sequence a fresh call would run. The result is NOT
    cached against the worktree directory, so crash-recovery resolves
    the base from "what origin looks like now". Callers should not
    rely on a stable per-worktree ``base_branch`` over time.
    """

    path: Path
    base_branch: str


class WorktreeManager:
    """Create + cleanup per-ticket git worktrees.

    ``role_token`` is the role-bot's GitHub installation token. When
    provided, every ``git`` / ``uv`` subprocess this manager spawns
    receives ``GH_TOKEN=<role_token>`` via
    :func:`foreman._env_filter.filtered_subprocess_env`. Without it
    those subprocesses inherit the daemon's parent ``GH_TOKEN``
    (CI runner, dev shell, whichever role last set one), so
    ``git fetch`` / ``git push`` against private repos authenticate
    as the wrong identity — same anti-attribution-leak motivation
    as the role-module subprocess fix in Stage 3e of the v3 rescue.

    ``role_token`` is OPTIONAL so the WorktreeManager remains usable
    from manual CLI invocations and the existing test suite (which
    constructs the manager without a role identity in scope).
    """

    def __init__(
        self,
        worktrees_root: Path,
        *,
        role_token: str | None = None,
    ) -> None:
        self.worktrees_root = worktrees_root
        self._role_token = role_token

    def _env(self) -> dict[str, str]:
        """Return the subprocess env for this manager's git/uv calls.

        Wraps :func:`filtered_subprocess_env` so the role token (if any)
        is forwarded as ``GH_TOKEN`` and Foreman's foreign-worktree
        env-leak blocklist (``VIRTUAL_ENV`` etc.) is applied.
        """
        return filtered_subprocess_env(role_token=self._role_token)

    def _self_heal_orphaned_branch(self, *, clone_path: Path, branch_name: str) -> None:
        """foreman#220: clear stranded local branch + stale worktree
        metadata before a ``git worktree add -b <branch>`` call.

        Failure mode the daemon hits after every ``docker compose
        restart``: the clone (``/foreman/repos/<project>``) lives on a
        persistent named volume, so its ``.git/worktrees/<name>/``
        metadata and local ``foreman/{impl,issue}-N`` branches survive
        rebuild. But worktree directories live on the ephemeral
        container filesystem (``/root/.foreman/worktrees/``) and get
        wiped. The next ``git worktree add -b <branch>`` exits 128/255
        with "fatal: a branch named '<branch>' already exists" because
        the branch name is taken by an orphan.

        Two-step defensive cleanup:

        1. ``git worktree prune`` — clears any worktree metadata entry
           whose pointed-to directory no longer exists. Safe: git only
           drops entries it already marks "prunable" (sees the missing
           dir on its own); it never removes live worktrees.
        2. ``git branch -D <branch>`` — deletes the orphan local branch
           if present. Best-effort: ``-D`` is non-interactive and the
           call uses ``check=False`` because the branch may legitimately
           not exist (clean state, fresh ticket). The actual
           ``git worktree add -b`` that follows will recreate the
           branch from the same base ref.

        The recovery is paired so callers don't have to think about
        which fault mode (stale metadata, orphan branch, or both) they
        might be hitting. Calling this in clean state is a cheap no-op.
        """
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=clone_path,
            check=True,
            capture_output=True,
            env=self._env(),
        )
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=clone_path,
            check=False,
            capture_output=True,
            env=self._env(),
        )

    def create(
        self,
        clone_path: Path,
        repo_slug: str,
        ticket_id: int,
        *,
        dev_base_branch: str | None = None,
        repo_url: str | None = None,
    ) -> Path:
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

        ``dev_base_branch`` (Foreman issue #16) lets operators override the
        default-branch resolution when the project's active development line
        lives on a feature branch rather than on ``main``. Concrete case:
        foreman itself during its walking-skeleton phase — ``origin/main``
        had only the initial scaffold commit, while all real work was on
        ``feat/walking-skeleton``. Without this knob the Planner would
        branch from origin/main and read a scaffold-only worktree, producing
        a spec saying "the prerequisite modules don't exist yet" — true for
        origin/main, wrong for the active dev line. When set to a non-None
        value the override is treated as authoritative; we do not second-
        guess it with auto-detection (explicit operator config only in v1).

        Before creating the branch we ``git fetch origin <base-branch>``
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
        # Container first-run bootstrap: if `clone_path` doesn't exist
        # yet (empty `foreman-repos` volume), clone it from `repo_url`
        # before any worktree-add operation. Idempotent: no-op when the
        # clone already exists. `repo_url` is optional so legacy
        # host-side callers (which pre-clone manually) still work.
        if repo_url is not None:
            ensure_clone(repo_url=repo_url, clone_path=clone_path)

        wt_path = self.worktrees_root / repo_slug / f"issue-{ticket_id}"
        if wt_path.exists():
            return wt_path
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        branch = spec_branch(ticket_id)
        base_branch = dev_base_branch or _resolve_default_branch(
            clone_path, role_token=self._role_token
        )
        _fetch_origin_branch(clone_path, base_branch, role_token=self._role_token)
        # foreman#220: clear orphan branch + stale worktree metadata
        # left over from a prior container generation (ephemeral
        # worktree dir vs persistent clone state). Cheap no-op on a
        # clean clone; recovers transparently when stranded.
        self._self_heal_orphaned_branch(clone_path=clone_path, branch_name=branch)
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                branch,
                str(wt_path),
                f"origin/{base_branch}",
            ],
            cwd=clone_path,
            check=True,
            capture_output=True,
            text=True,
            env=self._env(),
        )
        _maybe_sync_worktree_deps(wt_path, role_token=self._role_token)
        return wt_path

    def create_impl(
        self,
        clone_path: Path,
        repo_slug: str,
        ticket_id: int,
        *,
        repo_url: str | None = None,
    ) -> ImplWorktreeResult:
        """Create a fresh worktree for the Worker's impl branch.

        Path: ``<worktrees_root>/<repo_slug>/impl-<N>/`` — deliberately a
        sibling of ``issue-<N>/`` (the spec-side worktree the Planner /
        Reviewer / Fixer share). Keeping the two worktrees separate lets
        the Worker's branch evolve independently of any in-flight Fixer
        edits to the spec doc — and lets a future ticket's Worker still
        find a clean tree even if a Fixer left the spec worktree mid-edit.
        Sharing one worktree would force the Worker to git stash / reset
        every time it inherited a Fixer's WIP state.

        Branch: ``foreman/impl-<N>``. Base is chosen by probing origin
        state at call time, in two cases:

        - **Stacked path** (D1): if ``origin/foreman/issue-<N>`` exists,
          the new impl branch is based on it. The returned
          ``base_branch`` is ``"foreman/issue-<N>"``, so the impl PR
          opens stacked on the spec PR — the spec PR remains
          independently reviewable + mergeable.
        - **Fallback path** (issue #48): if ``origin/foreman/issue-<N>``
          is MISSING but the spec doc
          (``docs/superpowers/specs/foreman-issue-<N>-spec.md``) is
          present on ``origin/<default-branch>``, the new impl branch
          is based on ``origin/<default>``. The returned
          ``base_branch`` is the default branch name. The impl PR
          opens with ``base=<default>`` from the start; no retarget
          step is needed because there is no stacking. This covers
          spec-PR-merged-with-``--delete-branch``, "Automatically
          delete head branches", and the manual-operator-deleted-the-
          branch cases. The fallback fires ONLY when the spec content
          demonstrably exists on default — it is not a silent
          reach-around for "spec was never produced".

        If BOTH probes fail (spec branch gone AND spec doc absent from
        default), raises :class:`RuntimeError` naming the missing
        branch, the default branch checked, the expected spec doc
        path, and referencing issue #48 — strictly more actionable
        than the deep ``CalledProcessError`` the bare ``git worktree
        add`` would have raised.

        Idempotent: if ``impl-<N>/`` already exists we return an
        :class:`ImplWorktreeResult` for it without re-running ``git
        worktree add`` or ``uv sync``. The ``base_branch`` on the
        idempotent return is RECOMPUTED from current origin state
        (same probe sequence as a fresh run) — so crash-recovery
        resolves the base from "what origin looks like now", not from
        any cached metadata file. Callers should not assume a stable
        per-worktree ``base_branch`` across calls.

        Same fetch + uv-sync best-effort discipline as :meth:`create`.
        The fetch targets ``foreman/issue-<N>`` so the local origin ref
        is current — without this the probe would see a stale ref.
        The fallback path also fetches the default branch before
        probing the spec doc.

        Callers (notably ``run_worker``) must pass the returned
        ``base_branch`` as the impl PR's ``base`` — never hard-code
        ``foreman/issue-<N>`` at the call site, because that's only
        correct on the stacked path.
        """
        # Container first-run bootstrap: see WorktreeManager.create()
        # docstring for the contract. ensure_clone is a no-op when the
        # clone already exists; `repo_url=None` keeps legacy host-side
        # callers (which pre-clone manually) unchanged.
        if repo_url is not None:
            ensure_clone(repo_url=repo_url, clone_path=clone_path)

        impl_branch_name = impl_branch(ticket_id)
        spec_branch_name = spec_branch(ticket_id)
        default_branch = _resolve_default_branch(clone_path, role_token=self._role_token)

        wt_path = self.worktrees_root / repo_slug / f"impl-{ticket_id}"
        if wt_path.exists():
            # Idempotent re-call: recompute base_branch from current
            # origin state. Cheap (two local git probes, one
            # best-effort fetch each) and avoids stale-cached-answer
            # surprises across crash-recovery boundaries.
            base_branch_for_pr = self._resolve_impl_base_branch(
                clone_path=clone_path,
                ticket_id=ticket_id,
                spec_branch_name=spec_branch_name,
                default_branch=default_branch,
            )
            return ImplWorktreeResult(path=wt_path, base_branch=base_branch_for_pr)

        wt_path.parent.mkdir(parents=True, exist_ok=True)

        # Resolve the right base ref + the base_branch the impl PR
        # should target. The decision is centralized so the idempotent
        # branch above can reuse it.
        base_ref, base_branch_for_pr = self._resolve_impl_base_ref_and_branch(
            clone_path=clone_path,
            ticket_id=ticket_id,
            spec_branch_name=spec_branch_name,
            default_branch=default_branch,
        )

        # foreman#220: clear orphan branch + stale worktree metadata
        # left over from a prior container generation (ephemeral
        # worktree dir vs persistent clone state). Cheap no-op on a
        # clean clone; recovers transparently when stranded.
        self._self_heal_orphaned_branch(
            clone_path=clone_path, branch_name=impl_branch_name
        )
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                impl_branch_name,
                str(wt_path),
                base_ref,
            ],
            cwd=clone_path,
            check=True,
            capture_output=True,
            text=True,
            env=self._env(),
        )
        _maybe_sync_worktree_deps(wt_path, role_token=self._role_token)
        return ImplWorktreeResult(path=wt_path, base_branch=base_branch_for_pr)

    def _resolve_impl_base_ref_and_branch(
        self,
        *,
        clone_path: Path,
        ticket_id: int,
        spec_branch_name: str,
        default_branch: str,
    ) -> tuple[str, str]:
        """Decide what ref to branch the impl worktree from and what
        branch the impl PR should target.

        Returns ``(base_ref, base_branch_for_pr)`` — ``base_ref`` is the
        ref name passed to ``git worktree add`` (e.g.
        ``"origin/foreman/issue-42"`` or ``"origin/main"``);
        ``base_branch_for_pr`` is the bare branch name the impl PR's
        ``base`` should be set to.

        Decision order:

        1. Refresh ``origin/<spec-branch>`` (best-effort). Probe — if
           the ref now resolves, take the stacked path.
        2. Else refresh ``origin/<default>`` (best-effort). Probe the
           spec doc on default — if present, take the fallback path.
        3. Else raise :class:`RuntimeError` per the contract documented
           on :meth:`create_impl`.
        """
        # Refresh the spec branch from origin so the probe sees the
        # Planner's latest push, not a stale ref. Best-effort — same
        # rationale as ``create``'s default-branch fetch.
        _fetch_origin_branch(clone_path, spec_branch_name, role_token=self._role_token)

        if _origin_branch_exists(clone_path, spec_branch_name, role_token=self._role_token):
            return f"origin/{spec_branch_name}", spec_branch_name

        # Spec branch is missing. Refresh the default branch and check
        # whether the spec doc landed on it.
        _fetch_origin_branch(clone_path, default_branch, role_token=self._role_token)
        if _spec_doc_on_origin_default(
            clone_path, default_branch, ticket_id, role_token=self._role_token
        ):
            return f"origin/{default_branch}", default_branch

        spec_doc_path = f"docs/superpowers/specs/foreman-issue-{ticket_id}-spec.md"
        raise RuntimeError(
            f"Cannot create impl worktree for issue #{ticket_id}: "
            f"origin/{spec_branch_name} is missing AND the spec doc "
            f"{spec_doc_path} is not present on origin/{default_branch}. "
            f"The spec PR may not have been opened, or it was closed "
            f"without merging. See issue #48 for the design rationale "
            f"on this fallback path."
        )

    def _resolve_impl_base_branch(
        self,
        *,
        clone_path: Path,
        ticket_id: int,
        spec_branch_name: str,
        default_branch: str,
    ) -> str:
        """Idempotent-call variant of
        :meth:`_resolve_impl_base_ref_and_branch` that returns only the
        ``base_branch_for_pr`` — used when the worktree already exists
        and we just need to recompute the impl PR base from current
        origin state.
        """
        _, base_branch_for_pr = self._resolve_impl_base_ref_and_branch(
            clone_path=clone_path,
            ticket_id=ticket_id,
            spec_branch_name=spec_branch_name,
            default_branch=default_branch,
        )
        return base_branch_for_pr

    def attach(
        self,
        clone_path: Path,
        repo_slug: str,
        ticket_id: int,
        *,
        repo_url: str | None = None,
    ) -> Path:
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
        # Container first-run bootstrap — handles the edge case of a
        # post-`down -v` restart where the foreman-repos volume is empty
        # but the daemon's first observed state of this ticket is already
        # post-Planner (awaiting-review / awaiting-fix). See
        # WorktreeManager.create() docstring for the contract.
        if repo_url is not None:
            ensure_clone(repo_url=repo_url, clone_path=clone_path)

        wt_path = self.worktrees_root / repo_slug / f"issue-{ticket_id}"
        if wt_path.exists():
            return wt_path
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        branch = spec_branch(ticket_id)
        if not _local_branch_exists(clone_path, branch, role_token=self._role_token):
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
                env=self._env(),
            )
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=clone_path,
            check=True,
            capture_output=True,
            text=True,
            env=self._env(),
        )
        _maybe_sync_worktree_deps(wt_path, role_token=self._role_token)
        return wt_path

    def attach_impl(
        self,
        clone_path: Path,
        repo_slug: str,
        ticket_id: int,
        *,
        repo_url: str | None = None,
    ) -> Path:
        """Attach a worktree to an existing ``foreman/impl-<N>`` branch.

        Read-side counterpart to :meth:`create_impl`. After the Worker pushes
        ``foreman/impl-<N>`` and opens an impl PR, downstream roles (the
        Reviewer running on the impl PR, eventually the impl-Fixer) need
        their own worktree pinned at ``impl-<N>/`` checked out on the impl
        branch. They must NOT recreate the branch (the Worker already
        authored it) and they must NOT share the Worker's worktree (which
        may be cleaned up or carry uncommitted state).

        Path: ``<worktrees_root>/<repo_slug>/impl-<N>/`` — sibling of
        ``issue-<N>/`` (the spec-side worktree). Idempotent: if the
        worktree path already exists, it is returned untouched.

        Falls back to ``git fetch origin foreman/impl-<N>`` when the local
        branch is absent — defense-in-depth mirror of :meth:`attach`. On
        a fresh clone where only the remote ref exists, this lets
        ``git worktree add`` resolve the branch.

        Like :meth:`attach`, this best-effort syncs the worktree's ``.venv``
        afterward so the target repo's pre-push hook can ``uv run --no-sync``
        without exploding.
        """
        # Container first-run bootstrap. See WorktreeManager.create().
        if repo_url is not None:
            ensure_clone(repo_url=repo_url, clone_path=clone_path)

        wt_path = self.worktrees_root / repo_slug / f"impl-{ticket_id}"
        if wt_path.exists():
            return wt_path
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        branch = impl_branch(ticket_id)
        if not _local_branch_exists(clone_path, branch, role_token=self._role_token):
            # Branch isn't local yet — fetch the remote ref so worktree add
            # can resolve it. The Worker pushes the branch, so origin should
            # have it. We tolerate fetch failure here and let the worktree
            # add command surface a clearer error.
            subprocess.run(
                ["git", "fetch", "origin", branch],
                cwd=clone_path,
                check=False,
                capture_output=True,
                text=True,
                env=self._env(),
            )
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=clone_path,
            check=True,
            capture_output=True,
            text=True,
            env=self._env(),
        )
        _maybe_sync_worktree_deps(wt_path, role_token=self._role_token)
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
            env=self._env(),
        )


def _resolve_default_branch(clone_path: Path, *, role_token: str | None = None) -> str:
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

    ``role_token``, when supplied, is forwarded as ``GH_TOKEN`` so the
    git invocation authenticates as the role bot instead of inheriting
    whatever ``GH_TOKEN`` the daemon's parent shell had.
    """
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=clone_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(role_token=role_token),
    )
    if result.returncode != 0:
        return "main"
    ref = result.stdout.strip()
    prefix = "origin/"
    if ref.startswith(prefix):
        ref = ref[len(prefix) :]
    return ref or "main"


def _fetch_origin_branch(
    clone_path: Path, branch: str, *, role_token: str | None = None
) -> None:
    """Best-effort ``git fetch origin <branch>`` to refresh the local origin ref.

    Without this, ``origin/<branch>`` may be stale and basing the new spec
    branch on it would re-introduce drift from the other direction (origin
    moved forward; local origin ref didn't). On network failure we print a
    warning to stderr and continue — the subsequent worktree-add may still
    succeed from the cached origin ref, and a hard failure here would block
    ticket execution on transient connectivity issues.

    Uses the same env filter as the worktree-add and ``uv sync`` calls so
    we don't leak ``VIRTUAL_ENV`` etc. into git hook invocations.
    ``role_token`` overrides ``GH_TOKEN`` so the fetch authenticates as
    the role bot rather than inheriting the daemon's identity.
    """
    result = subprocess.run(
        ["git", "fetch", "--quiet", "origin", branch],
        cwd=clone_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(role_token=role_token),
    )
    if result.returncode == 0:
        return
    # foreman#122: rc=128 with stderr "couldn't find remote ref" means
    # the branch was deleted on origin (e.g., spec PR merged + GitHub
    # auto-deleted the head branch). The local
    # ``refs/remotes/origin/<branch>`` ref still points at the last
    # fetched tip, so ``_origin_branch_exists`` reads the stale cache
    # and lies — which breaks WorktreeManager's fallback gate to the
    # default branch and Worker.create_pull faceplants on base=invalid
    # 422. Prune the stale ref so subsequent checks see the truth.
    stderr = result.stderr or ""
    if result.returncode == 128 and "couldn't find remote ref" in stderr.lower():
        subprocess.run(
            ["git", "update-ref", "-d", f"refs/remotes/origin/{branch}"],
            cwd=clone_path,
            check=False,
            capture_output=True,
            text=True,
            env=filtered_subprocess_env(role_token=role_token),
        )
        print(
            f"[foreman.worktree] note: remote branch origin/{branch} no "
            f"longer exists (likely PR merged + auto-deleted); pruned "
            f"stale local ref so the fallback gate fires.",
            file=sys.stderr,
        )
        return
    # Other failures (transient network, auth, etc.): keep the original
    # best-effort tolerance — the cached origin ref may still be usable
    # for the subsequent worktree-add or rev-parse, and hard-failing
    # here would block ticket execution on transient issues.
    print(
        f"[foreman.worktree] warning: git fetch origin {branch} failed in "
        f"{clone_path} (rc={result.returncode}); proceeding with cached "
        f"origin ref. stderr:\n{stderr.strip()}",
        file=sys.stderr,
    )


def _local_branch_exists(
    clone_path: Path, branch: str, *, role_token: str | None = None
) -> bool:
    """Return True if ``branch`` exists as a local ref in ``clone_path``.

    Purely local probe (no network), but still routes through the env
    filter so a leaked ``VIRTUAL_ENV`` can't mis-direct any git hook
    that fires inside the foreign clone. ``role_token`` is accepted
    for symmetry with the manager's other helpers — git ``show-ref``
    against local refs doesn't authenticate, so the token has no
    behavioral effect here, but threading it consistently keeps the
    call shape uniform across helpers.
    """
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=clone_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(role_token=role_token),
    )
    return result.returncode == 0


def _origin_branch_exists(
    clone_path: Path, branch: str, *, role_token: str | None = None
) -> bool:
    """Return True if ``origin/<branch>`` resolves as a remote-tracking ref
    in ``clone_path``.

    Uses ``git rev-parse --verify --quiet`` against
    ``refs/remotes/origin/<branch>`` — silent, no network, rc 0 iff the
    ref resolves locally. Callers run a best-effort fetch first so the
    cached ref is current before probing.

    Sibling to :func:`_local_branch_exists` (which checks local heads).
    Both use the env filter so we don't leak ``VIRTUAL_ENV`` into git
    invocations in the foreign clone.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
        cwd=clone_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(role_token=role_token),
    )
    return result.returncode == 0


def _spec_doc_on_origin_default(
    clone_path: Path,
    default_branch: str,
    ticket_id: int,
    *,
    role_token: str | None = None,
) -> bool:
    """Return True if the spec doc for ``ticket_id`` exists on
    ``origin/<default_branch>``.

    Uses ``git cat-file -e origin/<default>:<spec_doc_path>`` — exits 0
    iff the path exists in that tree. No checkout, no working-tree
    mutation, no network round-trip. Stronger evidence for "the spec
    content is on the branch we'd be branching off" than the PyGithub
    "was the PR merged?" API call would be (a PR merged into the wrong
    base, hand-edited target, or closed without merging would all fool
    the API check while this local probe gives the right answer).

    The spec doc path matches what the Planner writes
    (``docs/superpowers/specs/foreman-issue-<N>-spec.md``).
    """
    spec_doc_path = f"docs/superpowers/specs/foreman-issue-{ticket_id}-spec.md"
    result = subprocess.run(
        ["git", "cat-file", "-e", f"origin/{default_branch}:{spec_doc_path}"],
        cwd=clone_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(role_token=role_token),
    )
    return result.returncode == 0


def _maybe_sync_worktree_deps(
    worktree_path: Path, *, role_token: str | None = None
) -> None:
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
        env=filtered_subprocess_env(role_token=role_token),
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
