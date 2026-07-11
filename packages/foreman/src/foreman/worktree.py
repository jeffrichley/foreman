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
from typing import Literal

from foreman._env_filter import filtered_subprocess_env
from foreman.branches import impl_branch, spec_branch


def ensure_clone(*, repo_url: str, clone_path: Path, token: str | None = None) -> None:
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
        token: Optional GitHub App installation token. When set and
            ``repo_url`` starts with ``"https://"``, the token is
            embedded in the clone URL as
            ``https://x-access-token:<token>@...`` so git authenticates
            without a credential helper. Non-HTTPS URLs (local paths,
            ``file://``, SSH) pass through unchanged.

    Raises:
        RuntimeError: if ``clone_path`` exists but is not a git
            repository (no ``.git`` directory). The operator must remove
            or repair the path manually before restarting the daemon.
        subprocess.CalledProcessError: if ``git clone`` fails.
    """
    if clone_path.exists() and not (clone_path / ".git").exists():
        raise RuntimeError(
            f"ensure_clone: {clone_path} exists but is not a git "
            f"repository (no .git directory). Remove the path or repair it "
            f"manually before restarting the daemon."
        )
    if (clone_path / ".git").exists():
        return
    clone_path.parent.mkdir(parents=True, exist_ok=True)
    clone_url = repo_url
    if token is not None and repo_url.startswith("https://"):
        clone_url = f"https://x-access-token:{token}@" + repo_url[len("https://") :]
    subprocess.run(
        ["git", "clone", clone_url, str(clone_path)],
        check=True,
        capture_output=True,
    )


@dataclass(frozen=True)
class ImplWorktreeResult:
    """Outcome of :meth:`WorktreeManager.create_impl`.

    Carries both the worktree filesystem path and the branch the impl
    PR should target as its base.

    Pre-foreman#341 ``base_branch`` could be ``"foreman/issue-<N>"``
    when the impl PR stacked on the spec PR (a vestigial design from
    before the v4 ``SpecReviewState`` merged the spec PR into ``main``
    before transitioning to ``ImplementingState``). After foreman#341,
    by the time the Worker runs the spec doc is already on ``main``,
    so the impl PR targets ``main`` (or the project's configured
    ``dev_base_branch``) directly. ``base_branch`` is therefore always
    the project's resolved dev base — never the spec branch.

    On idempotent re-call (the impl worktree path already exists),
    ``base_branch`` is recomputed from the same input
    (``dev_base_branch`` if provided, otherwise the clone's default
    branch). Callers should not rely on a stable per-worktree
    ``base_branch`` across calls if ``dev_base_branch`` changes
    between them.
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
        """Initialize the manager, storing the worktrees root and optional role token.

        ``role_token`` (if given) is forwarded as ``GH_TOKEN`` on every
        git/uv subprocess this manager spawns — see the class docstring
        for why that matters.
        """
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
        """Clear a stranded local branch + stale worktree metadata (foreman#220).

        Runs before a ``git worktree add -b <branch>`` call.

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
            if _worktree_gitdir_is_valid(wt_path, role_token=self._role_token):
                return wt_path
            # Foreign/unresolvable gitdir from a prior environment (e.g. a
            # Windows host path inside a Linux container). Tear down so the
            # creation block below starts from a clean slate.
            shutil.rmtree(wt_path)
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
        dev_base_branch: str | None = None,
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

        Branch: ``foreman/impl-<N>``, based on ``origin/<dev_base_branch>``
        (or ``origin/<default-branch>`` when ``dev_base_branch`` is
        ``None``).

        foreman#341: pre-v4 this method probed for the spec branch and
        the spec doc on default, stacking the impl branch on
        ``origin/foreman/issue-<N>`` when present so the impl PR could
        stack on the spec PR. That design pre-dated v4's
        ``SpecReviewState``, which merges the spec PR into the dev base
        BEFORE transitioning to ``ImplementingState``. By the time the
        Worker runs, the spec is on the dev base already, so the impl
        PR should target the dev base directly. Stacking the impl PR
        on the (orphan) spec branch caused PR #339 — merging the impl
        PR through the GitHub UI landed changes on the spec branch
        instead of ``main``. The probe / fallback logic is now gone;
        the base is read from ``dev_base_branch`` (or the default
        branch) like :meth:`create` already does for spec worktrees.

        Idempotent: if ``impl-<N>/`` already exists we return an
        :class:`ImplWorktreeResult` for it without re-running ``git
        worktree add`` or ``uv sync``. The ``base_branch`` on the
        idempotent return is recomputed the same way as the fresh
        path — read from the ``dev_base_branch`` argument when
        provided, otherwise resolved from the clone's default branch.

        Same fetch + uv-sync best-effort discipline as :meth:`create`.

        Callers (notably ``run_worker``) must pass the returned
        ``base_branch`` as the impl PR's ``base``.
        """
        # Container first-run bootstrap: see WorktreeManager.create()
        # docstring for the contract. ensure_clone is a no-op when the
        # clone already exists; `repo_url=None` keeps legacy host-side
        # callers (which pre-clone manually) unchanged.
        if repo_url is not None:
            ensure_clone(repo_url=repo_url, clone_path=clone_path)

        impl_branch_name = impl_branch(ticket_id)
        base_branch = dev_base_branch or _resolve_default_branch(
            clone_path, role_token=self._role_token
        )

        wt_path = self.worktrees_root / repo_slug / f"impl-{ticket_id}"
        if wt_path.exists():
            if _worktree_gitdir_is_valid(wt_path, role_token=self._role_token):
                # Idempotent re-call: recompute base_branch from the same
                # input as a fresh call (``dev_base_branch`` arg or default
                # branch resolution). No probing of the spec branch — the
                # impl PR's base is determined entirely by project config.
                return ImplWorktreeResult(path=wt_path, base_branch=base_branch)
            # Foreign/unresolvable gitdir from a prior environment (e.g. a
            # Windows host path inside a Linux container). Tear down so the
            # creation block below starts from a clean slate.
            shutil.rmtree(wt_path)

        wt_path.parent.mkdir(parents=True, exist_ok=True)

        # Best-effort fetch the base branch so the local origin ref is
        # current before we branch off it. Same discipline as
        # :meth:`create` uses for the spec worktree (see
        # ``_fetch_origin_branch`` for the foreman#122 self-heal that
        # also fires here if the configured base branch was deleted
        # upstream).
        _fetch_origin_branch(clone_path, base_branch, role_token=self._role_token)

        # foreman#220: clear orphan branch + stale worktree metadata
        # left over from a prior container generation (ephemeral
        # worktree dir vs persistent clone state). Cheap no-op on a
        # clean clone; recovers transparently when stranded.
        self._self_heal_orphaned_branch(clone_path=clone_path, branch_name=impl_branch_name)
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                impl_branch_name,
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
        return ImplWorktreeResult(path=wt_path, base_branch=base_branch)

    def attach(
        self,
        clone_path: Path,
        repo_slug: str,
        ticket_id: int,
        *,
        repo_url: str | None = None,
        target: Literal["spec_pr", "impl_pr"] = "spec_pr",
    ) -> Path:
        """Attach a worktree to an existing role branch.

        Used by downstream roles (Reviewer / Fixer / Worker) that should NOT
        create a new branch — the upstream role (Planner for spec, Worker
        for impl) already opened the branch and pushed it. Idempotent: if
        the worktree path already exists, it is returned untouched.

        The ``target`` kwarg selects which branch convention the attach
        targets:

        - ``"spec_pr"`` (default — back-compat for every existing caller):
          attach to ``foreman/issue-<N>`` in the worktree at
          ``<root>/<repo_slug>/issue-<N>/``. The Planner opened the
          branch.
        - ``"impl_pr"``: attach to ``foreman/impl-<N>`` in the worktree at
          ``<root>/<repo_slug>/impl-<N>/`` (a sibling of ``issue-<N>/``).
          The Worker opened the branch — this is the path the impl-side
          Fixer must take so its edits + commits land on the impl PR's
          branch. Pre-Phase 8d.23 the Fixer hardcoded the spec path even
          when called with ``target='impl_pr'`` — the algokit#21 dogfood
          surfaced the bug: the Fixer fetched the impl PR (8d.21 made
          PR-lookup target-aware) but committed to ``foreman/issue-21``,
          missing the impl PR entirely. This kwarg is the missing other
          half of the 8d.21 fix.

        For both targets we fall back to a tracking-branch fetch if the
        local branch does not yet exist (the role may run on a clone
        where the branch only lives on the remote — common after
        container restart).

        Like :meth:`create`, this best-effort syncs the worktree's
        ``.venv`` afterward so the target repo's pre-push hook can
        ``uv run --no-sync`` without exploding.

        The impl path delegates to :meth:`attach_impl`, which already
        encapsulates the existing-branch attach semantics for the impl
        case (introduced in foreman#41 for the Reviewer-on-impl flow).
        Callers may continue invoking :meth:`attach_impl` directly — both
        entry points are supported.
        """
        if target == "impl_pr":
            return self.attach_impl(
                clone_path=clone_path,
                repo_slug=repo_slug,
                ticket_id=ticket_id,
                repo_url=repo_url,
            )

        # Container first-run bootstrap — handles the edge case of a
        # post-`down -v` restart where the foreman-repos volume is empty
        # but the daemon's first observed state of this ticket is already
        # post-Planner (awaiting-review / awaiting-fix). See
        # WorktreeManager.create() docstring for the contract.
        if repo_url is not None:
            ensure_clone(repo_url=repo_url, clone_path=clone_path)

        wt_path = self.worktrees_root / repo_slug / f"issue-{ticket_id}"
        if wt_path.exists():
            if _worktree_gitdir_is_valid(wt_path, role_token=self._role_token):
                return wt_path
            # Foreign/unresolvable gitdir from a prior environment (e.g. a
            # Windows host path inside a Linux container). Tear down so the
            # attach block below starts from a clean slate.
            shutil.rmtree(wt_path)
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
            if _worktree_gitdir_is_valid(wt_path, role_token=self._role_token):
                return wt_path
            # Foreign/unresolvable gitdir from a prior environment (e.g. a
            # Windows host path inside a Linux container). Tear down so the
            # attach block below starts from a clean slate.
            shutil.rmtree(wt_path)
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

    def prune(
        self,
        *,
        project: str,
        issue_number: int,
        clone_path: Path,
    ) -> list[Path]:
        """Remove both ``issue-<N>`` and ``impl-<N>`` worktrees for this ticket.

        For each target path:

        1. If the path is a registered git worktree, try
           ``git worktree remove --force <path>``.
        2. If that fails (non-zero exit, or path isn't a registered
           worktree), fall back to ``shutil.rmtree(path, ignore_errors=False)``.
        3. If the path doesn't exist, silently skip it.

        Returns the list of paths that were actually removed. Used by
        ``foreman reset`` to wipe local debris for a stuck ticket.

        Args:
            project: Project slug; selects the ``<worktrees_root>/<project>/``
                subdirectory whose ``issue-<N>``/``impl-<N>`` siblings
                are candidates for removal.
            issue_number: Ticket number whose two sibling worktrees should
                be pruned.
            clone_path: The local clone whose registry tracks these worktrees.
                Required so ``git worktree remove`` consults the right
                ``.git/worktrees/`` entries — without this, git either errors
                (cwd isn't a repo) or hits the wrong registry. Comes from
                ``V4Config.projects[*].local_clone_path`` in production.
        """
        project_root = self.worktrees_root / project
        candidates = [
            project_root / f"issue-{issue_number}",
            project_root / f"impl-{issue_number}",
        ]
        removed: list[Path] = []
        for path in candidates:
            if not path.exists():
                continue
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(path)],
                    check=True,
                    capture_output=True,
                    env=self._env(),
                    cwd=clone_path,
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                # Either git rejected (not a registered worktree) or git
                # isn't on PATH. Fall back to rmtree.
                if path.exists():
                    try:
                        shutil.rmtree(path, ignore_errors=False)
                    except OSError as exc:
                        # Windows: daemon's open file handles or antivirus
                        # can keep files locked. Soft-fail so reset doesn't
                        # crash on the operator — they can retry once the
                        # locking process releases.
                        print(
                            f"warning: could not remove {path}: {exc}",
                            file=sys.stderr,
                        )
                        continue
            # Only count it as removed if the path is actually gone.
            # Guards against future changes (e.g. ignore_errors=True on
            # rmtree, or catching OSError above without continue) that
            # would otherwise falsely report removal.
            if not path.exists():
                removed.append(path)
        return removed


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


def _fetch_origin_branch(clone_path: Path, branch: str, *, role_token: str | None = None) -> None:
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
    # foreman#291: ``--prune`` evicts refs that no longer exist on
    # origin (e.g., feature branches deleted via PR merge with
    # auto-delete). Complementary to the explicit ``update-ref -d``
    # self-heal below (foreman#122): prune handles general cleanup
    # silently, while the self-heal targets the specific
    # "couldn't-find-remote-ref" rc=128 case that fires when the
    # named branch IS the one that disappeared.
    result = subprocess.run(
        ["git", "fetch", "--quiet", "--prune", "origin", branch],
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


def fetch_origin_default_branch(
    clone_path: Path,
    *,
    role_token: str | None = None,
) -> None:
    """Best-effort refresh of ``origin/<default-branch>``.

    Resolves the default-branch name via :func:`_resolve_default_branch`
    (which handles ``origin/HEAD`` missing by falling back to ``"main"``),
    then calls :func:`_fetch_origin_branch` against it. Same best-effort
    contract as the underlying helper: network failures are logged as
    warnings and swallowed.

    Called per-poll (throttled) by
    :class:`foreman.v4.clone_refresh.CloneRefresher` (foreman#407) to keep
    each project clone's ``origin/<default>`` ref fresh between role
    dispatches. (Supersedes the v3 ``reconciler.clone_refresh.OnPollFetch``
    of foreman#291, dropped in the #333 v4 cutover.)
    """
    default = _resolve_default_branch(clone_path, role_token=role_token)
    _fetch_origin_branch(clone_path, default, role_token=role_token)


def fetch_origin_branch(
    clone_path: Path,
    branch: str,
    *,
    role_token: str | None = None,
) -> None:
    """Best-effort refresh of ``origin/<branch>`` with stale-ref self-heal.

    Public wrapper around :func:`_fetch_origin_branch`. Same best-effort
    contract: network failures are logged as warnings and swallowed;
    rc=128 ``couldn't find remote ref`` triggers the foreman#122
    prune-stale-ref self-heal so a stale ``refs/remotes/origin/<branch>``
    is evicted at fetch time, not silently kept.

    Added in foreman#294 so :func:`foreman.roles.reviewer._get_pr_diff`
    can route through the shared self-heal instead of shelling out to
    ``git fetch`` directly.
    """
    _fetch_origin_branch(clone_path, branch, role_token=role_token)


def resolve_default_branch(
    clone_path: Path,
    *,
    role_token: str | None = None,
) -> str:
    """Return the repo's default branch name (fallback to ``"main"``).

    Public wrapper around :func:`_resolve_default_branch`. Reads
    ``origin/HEAD`` via ``git symbolic-ref``; falls back to ``"main"``
    if ``origin/HEAD`` is missing — existing behavior preserved.

    Added in foreman#294 so :func:`foreman.roles.reviewer._get_pr_diff`
    can resolve the fallback base ref without importing a private name.
    """
    return _resolve_default_branch(clone_path, role_token=role_token)


def _local_branch_exists(clone_path: Path, branch: str, *, role_token: str | None = None) -> bool:
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


def _worktree_gitdir_is_valid(worktree_path: Path, *, role_token: str | None = None) -> bool:
    """Return True iff ``git rev-parse --git-dir`` exits 0 inside the worktree.

    A worktree whose .git gitdir pointer resolves to a foreign/unresolvable
    path (e.g., a Windows host path inside a Linux container) causes rc=128
    ``fatal: not a git repository: <path>`` on any subsequent git operation.
    This probe detects that state before returning the worktree to the caller.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=worktree_path,
        check=False,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(role_token=role_token),
    )
    return result.returncode == 0


def _origin_branch_exists(clone_path: Path, branch: str, *, role_token: str | None = None) -> bool:
    """Return True if ``origin/<branch>`` resolves as a remote-tracking ref in ``clone_path``.

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


def _maybe_sync_worktree_deps(worktree_path: Path, *, role_token: str | None = None) -> None:
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
