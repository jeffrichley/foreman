"""Bubblewrap job-isolation launcher (foreman#job-sandbox-isolation).

The daemon spawns each role (Planner / Reviewer / Fixer / Worker) as a
``foreman <subcmd> ...`` subprocess. Historically that ran on the
daemon's shared system Python, so a job could reach out and corrupt the
daemon's own ``foreman`` install (the 2026-07-18 ``foreman.prompts``
incident), delete a sibling job's worktree, or read another role's
secret.

:class:`SandboxLauncher` wraps the role command in a ``bwrap`` invocation
that gives the job a positive, by-construction boundary: it can read and
write a shared uv cache (``/cache``, mounted read-write so jobs warm the
cache for later runs) and write only its own scratch worktree
(``/scratch``); the daemon's foreman source, the role PEM keys, the
credential vault, and sibling scratch dirs are simply not mounted.

:func:`preflight` runs a minimal sandbox at daemon startup so a host
without unprivileged user namespaces fails closed with an actionable
operator message instead of silently running jobs unsandboxed.

This module is pure orchestration plumbing under ``foreman.v4`` — it must
never import a v3-substrate module (import-lint R2).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

# Paths that must NEVER be bind-mounted into a job box. Asserted absent
# from the argv by the unit tests and by the hermetic integration test.
#   /run/secrets  — the role PEM keys (planner/reviewer/fixer/worker/
#                   orchestrator _pem) that mint installation tokens
#   /root/.foreman — the credential vault, projects.toml, keys/, backups
#   /app/source    — the daemon's own foreman source checkout
DAEMON_NEVER_BIND: tuple[str, ...] = ("/run/secrets", "/root/.foreman", "/app/source")

# The box's PATH. A fixed, minimal system PATH — the job's own venv under
# /scratch is activated by the role via ``uv run``, not by prepending to
# PATH here.
SANDBOX_STD_PATH: str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass(frozen=True)
class SandboxLauncher:
    """Pure builder of the ``bwrap`` argv that wraps a role command.

    A pure function of its inputs to an exact argv: no side effects, no
    filesystem access, trivially unit-testable. It owns the mount plan
    and the never-bind list. The dispatcher is responsible for creating
    the per-job ``scratch_dir`` on disk before calling
    :meth:`build_argv`.

    Attributes:
        cache_dir: Host path of the shared uv cache, bind-mounted
            read-write to ``cache_mount`` so jobs can warm the shared,
            content-addressed cache for later runs.
        bwrap_path: The ``bwrap`` binary (overridable for tests).
        cache_mount: In-box mountpoint for the shared cache.
        scratch_mount: In-box mountpoint for the job's writable scratch.
        extra_ro_binds: Additional host paths to bind read-only if they
            exist — the Claude CLI config + session dirs the role needs
            to make its LLM call. These are operational config, not the
            crown-jewel secrets in :data:`DAEMON_NEVER_BIND`.
    """

    cache_dir: str
    bwrap_path: str = "bwrap"
    cache_mount: str = "/cache"
    scratch_mount: str = "/scratch"
    extra_ro_binds: tuple[str, ...] = field(
        default_factory=lambda: ("/root/.claude", "/root/.claude-container")
    )

    def build_argv(
        self,
        *,
        role_token: str,
        scratch_dir: str,
        role_cmd: list[str],
        passthrough: Mapping[str, str] | None = None,
        repo_bind: tuple[str, str] | None = None,
        ro_file_binds: tuple[tuple[str, str], ...] = (),
    ) -> list[str]:
        """Return the ``bwrap`` argv wrapping ``role_cmd``.

        Args:
            role_token: The job's short-lived scoped GitHub role token;
                set as ``GH_TOKEN`` inside the box and nothing else
                secret.
            scratch_dir: Host path of this job's scratch dir, bind-mounted
                read-write to ``scratch_mount``. The role's
                ``WorktreeManager`` roots its worktree here via
                ``FOREMAN_WORKTREES_ROOT=/scratch``.
            role_cmd: The unwrapped role command
                (``["foreman", "implement", ...]``) to run inside the box.
            passthrough: Extra non-secret env vars to forward
                (state-instance id, session-resume ids, Claude config
                dir). The dispatcher curates this allowlist.
            repo_bind: ``(host_clone_path, box_repo_path)`` for the
                daemon's private per-job clone, bind-mounted READ-WRITE at
                ``box_repo_path`` — the exact in-box path the role's
                ``ProjectConfig.local_clone_path`` names. The role's
                normal ``git worktree add`` then links off this PRIVATE
                ``.git``, not the shared base repo (which is never
                mounted). ``None`` for pre-#556 / test callers.
            ro_file_binds: ``(host_path, box_path)`` pairs bind-mounted
                READ-ONLY — the ``FOREMAN_V4_CONFIG`` +
                ``FOREMAN_PROJECTS_PATH`` TOML files the role loads via
                ``load_v4_config`` / ``load_projects``. Small config
                files, not the crown-jewel secrets in
                :data:`DAEMON_NEVER_BIND`.

        Returns:
            The full argv: ``bwrap`` + namespace/mount/env flags + ``--``
            + ``role_cmd``.
        """
        # The box starts from a CLEARED environment (positive defense):
        # nothing from the daemon's env leaks in. Every var the job needs
        # is re-added explicitly below.
        setenv: dict[str, str] = {
            "PATH": SANDBOX_STD_PATH,
            "HOME": "/root",
            "PYTHONUNBUFFERED": "1",
            # Keep the job's Python off the daemon's user site-packages so
            # `import foreman` cannot resolve there by accident.
            "PYTHONNOUSERSITE": "1",
            # Scratch-rooted worktree: the role's WorktreeManager reads
            # this env var to place its per-ticket worktree under the
            # writable scratch mount (the only writable path in the box
            # besides the shared cache).
            "FOREMAN_WORKTREES_ROOT": self.scratch_mount,
            # uv reads/writes the shared content-addressed cache here
            # (mounted read-write — see cache_dir docstring).
            "UV_CACHE_DIR": self.cache_mount,
            "GH_TOKEN": role_token,
        }
        if passthrough:
            setenv.update(passthrough)

        argv: list[str] = [
            self.bwrap_path,
            "--clearenv",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--share-net",
            "--die-with-parent",
            "--tmpfs",
            "/tmp",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/bin",
            "/bin",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind-try",
            "/lib64",
            "/lib64",
            "--ro-bind-try",
            "/sbin",
            "/sbin",
            "--ro-bind",
            "/etc/resolv.conf",
            "/etc/resolv.conf",
            "--ro-bind",
            "/etc/ssl/certs",
            "/etc/ssl/certs",
            "--ro-bind-try",
            "/etc/ca-certificates",
            "/etc/ca-certificates",
            "--ro-bind-try",
            "/etc/gitconfig",
            "/etc/gitconfig",
            "--bind",
            self.cache_dir,
            self.cache_mount,
            "--bind",
            scratch_dir,
            self.scratch_mount,
        ]
        # RW: the daemon's private per-job clone, at the box path the
        # role's config already names (so the role's worktree-add path is
        # unchanged).
        if repo_bind is not None:
            host_clone, box_repo = repo_bind
            argv += ["--bind", host_clone, box_repo]
        # RO: the config + projects TOML the role loads at startup.
        for host_file, box_file in ro_file_binds:
            argv += ["--ro-bind", host_file, box_file]
        for extra in self.extra_ro_binds:
            argv += ["--ro-bind-try", extra, extra]
        for key in sorted(setenv):
            argv += ["--setenv", key, setenv[key]]
        argv += ["--chdir", self.scratch_mount, "--"]
        argv += role_cmd
        return argv


class SandboxUnavailableError(RuntimeError):
    """The bubblewrap sandbox cannot be created on this host.

    Raised by :func:`preflight` when a minimal ``bwrap`` boot fails —
    typically because the host lacks unprivileged user namespaces. The
    daemon fails closed on this rather than silently running role
    subprocesses unsandboxed.
    """


def _default_runner(argv: list[str]) -> tuple[int, str]:
    """Run ``argv`` discarding stdout but capturing stderr for diagnostics.

    Args:
        argv: The full command to run, ``bwrap`` and its flags.

    Returns:
        A ``(returncode, stderr)`` pair. ``stderr`` is bwrap's captured
        error output (not stripped), used to make a preflight failure
        actionable (userns blocked vs. seccomp vs. cgroup, etc.).
    """
    result = subprocess.run(  # argv is built here, not user input
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return result.returncode, result.stderr


def preflight(
    *,
    bwrap_path: str = "bwrap",
    runner: Callable[[list[str]], tuple[int, str]] | None = None,
) -> None:
    """Boot a minimal sandbox to prove the host supports it; else fail closed.

    Runs ``bwrap`` with the same namespaces the real jobs use, a RO
    ``/usr`` bind, a ``/tmp`` tmpfs, and ``/bin/true`` as the payload. A
    zero exit proves unprivileged user namespaces + the requested
    namespaces work under the container's default (non-privileged)
    security profile — the make-or-break capability the spike validated.

    Args:
        bwrap_path: The ``bwrap`` binary to probe.
        runner: Test seam ``(argv) -> (returncode, stderr)``. Defaults to
            a real ``subprocess.run`` that discards stdout but captures
            stderr.

    Raises:
        SandboxUnavailableError: on non-zero exit or any ``OSError`` from
            spawning ``bwrap`` (e.g. missing binary, permission denied),
            with an actionable operator message. bwrap's captured stderr
            is included when available so the operator can tell userns,
            seccomp, and cgroup failures apart.
    """
    run = runner if runner is not None else _default_runner
    argv = [
        bwrap_path,
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--ro-bind",
        "/usr",
        "/usr",
        "--tmpfs",
        "/tmp",
        "--",
        "/bin/true",
    ]
    try:
        rc, stderr = run(argv)
    except OSError as exc:
        raise SandboxUnavailableError(
            f"failed to spawn bwrap binary at {bwrap_path!r}: {exc}. Install "
            f"bubblewrap in the daemon image (apt-get install -y bubblewrap), "
            f"check its permissions, or set [sandbox].bwrap_path. Refusing to "
            f"run jobs unsandboxed."
        ) from exc
    if rc != 0:
        detail = stderr.strip() or "(no stderr captured)"
        raise SandboxUnavailableError(
            f"bwrap preflight exited {rc}: the host cannot create an "
            f"unprivileged user namespace sandbox. bwrap stderr: {detail}. "
            f"Verify unprivileged user namespaces are enabled "
            f"(kernel.unprivileged_userns_clone=1 / "
            f"user.max_user_namespaces > 0). Refusing to run jobs "
            f"unsandboxed; set [sandbox].allow_unsandboxed = true only for "
            f"local dev."
        )
