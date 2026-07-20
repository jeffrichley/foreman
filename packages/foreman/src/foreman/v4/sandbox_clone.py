"""Daemon-side private per-job clone prep + scratch cleanup (foreman#556).

The sandbox's enabled path gives each role its OWN self-contained clone
instead of a linked worktree that shares the base repo's ``.git`` (a
volume every job + the daemon poller touch). The trusted daemon — which
CAN see ``/foreman/repos`` — does a co-located ``git clone --local`` of
the base into the per-job scratch (hardlinked object store: ~zero disk,
free ONLY when scratch and base share a filesystem), then re-points the
private clone's ``origin`` at GitHub so the sealed box (which never sees
the base repo path) can fetch/push over the open network.

The box binds this private clone READ-WRITE at the in-box path the role's
``ProjectConfig.local_clone_path`` already names, so the role's normal
``WorktreeManager`` path runs unchanged — its ``git worktree add`` links
off a PRIVATE ``.git``.

Every subprocess this module spawns routes through
:func:`foreman._env_filter.filtered_subprocess_env`, mirroring
:mod:`foreman.worktree`'s ``WorktreeManager`` (``ensure_clone`` and
friends): without the filter a leaked ``VIRTUAL_ENV`` (the daemon's own
venv) could mis-direct a hook a git invocation triggers inside the
freshly-cloned foreign repo, and the dispatching role's ``GH_TOKEN`` is
threaded through so any hook that shells out to ``gh``/``git`` credential
helpers authenticates as the role bot, not the daemon.

Pure orchestration under ``foreman.v4`` — no v3-substrate import (R2).
``foreman._env_filter`` is part of the survival set (see the R2 contract
in ``pyproject.toml``), so importing it here is allowed.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from foreman._env_filter import filtered_subprocess_env

#: Role bases whose per-job scratch dirs a ticket accumulates. Terminal
#: cleanup removes ``<scratch_root>/<project>/<base>-<issue>`` for each.
_SANDBOX_ROLE_BASES: tuple[str, ...] = ("planner", "reviewer", "fixer", "worker")

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def sandbox_clone_argv(base_clone_path: Path, dest_clone_path: Path) -> list[str]:
    """Return the ``git clone --local`` argv (hardlinks when co-located).

    ``--local`` hardlinks the object store instead of copying — free, but
    ONLY when ``dest_clone_path`` sits on the same filesystem as
    ``base_clone_path`` (else git falls back to a cross-device copy, or
    fails outright on a hardlink attempt). Co-location is guaranteed by
    :attr:`SandboxConfig.scratch_root` defaulting under the repos volume.
    """
    return ["git", "clone", "--local", str(base_clone_path), str(dest_clone_path)]


def tokenized_origin_url(repo_url: str, role_token: str) -> str:
    """Embed the role token in an HTTPS remote URL for credential-less auth.

    Mirrors ``foreman.worktree.ensure_clone``: for an ``https://`` URL the
    token is inlined as ``https://x-access-token:<token>@...`` so ``git
    fetch`` / ``git push origin`` inside the box authenticate as the role
    bot without a credential helper. Non-HTTPS URLs pass through unchanged.
    """
    prefix = "https://"
    if repo_url.startswith(prefix):
        return f"https://x-access-token:{role_token}@" + repo_url[len(prefix) :]
    return repo_url


def _default_runner(argv: list[str], *, role_token: str | None) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` capturing output, raising on non-zero exit.

    Routes through :func:`foreman._env_filter.filtered_subprocess_env` —
    same discipline as ``foreman.worktree.WorktreeManager`` — so a leaked
    ``VIRTUAL_ENV`` can't mis-direct a hook fired inside the freshly
    cloned repo, and so ``GH_TOKEN`` matches the dispatching role's
    identity rather than whatever the daemon process happened to inherit.

    A ``git clone --local`` failure when ``dest_clone_path`` is NOT
    co-located with ``base_clone_path`` (the cross-device misconfiguration
    :func:`sandbox_clone_argv` warns about) raises
    :class:`subprocess.CalledProcessError` here with git's own
    ``fatal: Invalid cross-device link`` diagnostic captured in
    ``.stderr`` — already an actionable pointer at the operator's
    ``scratch_root`` misconfiguration, so no extra wrapping is added.
    """
    return subprocess.run(  # argv is built here, not user input
        argv,
        check=True,
        capture_output=True,
        text=True,
        env=filtered_subprocess_env(role_token=role_token),
    )


def prepare_sandbox_clone(
    *,
    base_clone_path: Path,
    dest_clone_path: Path,
    repo_url: str,
    role_token: str,
    runner: Runner | None = None,
) -> None:
    """Ensure a private per-job clone exists at ``dest_clone_path``.

    Idempotent: when ``dest_clone_path/.git`` already exists (a retry
    reusing the same scratch) the clone step is skipped. The ``origin``
    re-point runs EVERY call so a rotated short-lived role token is always
    refreshed on the private clone's remote.

    Args:
        base_clone_path: The project's shared base clone (e.g.
            ``/foreman/repos/<project>``); read-only source for the local
            clone. Never mounted into the box.
        dest_clone_path: Where the private clone lands — must be co-located
            with ``base_clone_path`` for the hardlink (see
            :func:`sandbox_clone_argv`).
        repo_url: The GitHub HTTPS URL (``https://github.com/<owner>/<name>.git``)
            the private clone's ``origin`` is re-pointed at.
        role_token: The dispatching role's short-lived GitHub token, inlined
            into the ``origin`` URL for network fetch/push, and forwarded as
            ``GH_TOKEN`` on the subprocess env (see :func:`_default_runner`).
        runner: Test seam ``(argv) -> CompletedProcess``. Defaults to a real
            ``subprocess.run(check=True)`` filtered through
            :func:`foreman._env_filter.filtered_subprocess_env`.
    """
    run = (
        runner if runner is not None else functools.partial(_default_runner, role_token=role_token)
    )
    if not (dest_clone_path / ".git").exists():
        dest_clone_path.parent.mkdir(parents=True, exist_ok=True)
        run(sandbox_clone_argv(base_clone_path, dest_clone_path))
    run(
        [
            "git",
            "-C",
            str(dest_clone_path),
            "remote",
            "set-url",
            "origin",
            tokenized_origin_url(repo_url, role_token),
        ]
    )
    # Freshness chokepoint (foreman clone-freshness design): after the local
    # hardlink clone (which reflects the base's snapshot) and the origin
    # re-point, fetch the current remote so the box sees every ref another
    # role may have just pushed. Blanket (all refs) — no branch to plumb, and
    # only missing objects download since the base seeds the rest. FAIL-CLOSED:
    # a fetch failure raises so the role never runs on a stale clone.
    run(["git", "-C", str(dest_clone_path), "fetch", "origin"])


def cleanup_ticket_scratch(
    *,
    scratch_root: Path,
    project: str,
    issue_number: int,
    role_bases: Sequence[str] = _SANDBOX_ROLE_BASES,
) -> list[Path]:
    """Remove every per-job scratch dir a ticket accumulated. Returns the removed paths.

    Called on terminal landing (Done/Failed/NeedsHelp). Because the clone's
    objects are hardlinked to the base, ``rmtree`` only drops the extra
    directory entries — it cannot corrupt the shared base repo. Silent when
    a dir is absent (a role that never ran, or already-cleaned).
    """
    removed: list[Path] = []
    project_root = scratch_root / project
    for base in role_bases:
        job_dir = project_root / f"{base}-{issue_number}"
        if not job_dir.exists():
            continue
        shutil.rmtree(job_dir, ignore_errors=True)
        if not job_dir.exists():
            removed.append(job_dir)
    return removed
