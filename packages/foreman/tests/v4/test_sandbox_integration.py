"""Hermetic real-bwrap integration + the permanent 2026-07-18 regression lock.

These exercise a REAL bwrap boot, so they only run on Linux with
unprivileged user namespaces. On any other runner (Windows dev box, CI
without nested userns) the whole module self-skips — the pure argv tests
in test_sandbox.py / test_sandbox_dispatch.py provide the cross-platform
coverage.

NOTE (brief bug fix): the originating brief's draft for
``test_scratch_is_writable_and_cache_is_readonly`` asserted a cache WRITE
was rejected as a read-only mount. That contradicts the actual mount plan
built by ``SandboxLauncher.build_argv`` (Task 2): the shared uv cache is
bound read-WRITE on purpose so jobs can warm it for later runs (see
``sandbox.py``'s module docstring, ``SandboxConfig.cache_dir``'s
docstring, and ``test_sandbox.py::test_argv_mounts_cache_rw_and_scratch_rw``,
which explicitly asserts the cache is never ``--ro-bind``-ed). Renamed to
``test_scratch_and_cache_are_both_writable`` and fixed to assert the real
contract: both scratch and cache writes succeed and land on the host.

Also folds in the Task-5 code-review gap (the on-disk log banner must
never carry the raw ``GH_TOKEN``) as
``test_sandboxed_dispatch_log_banner_redacts_gh_token`` below — it fits
this file's scope cleanly because it drives the real
``SubprocessRoleDispatcher`` + ``SandboxLauncher`` path end-to-end through
a real bwrap boot, rather than a mocked ``Popen`` (which is what
test_sandbox_dispatch.py already covers for the pure argv-shape case).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from foreman.v4.sandbox import (
    _OS_EXEC_ROOT_MOUNTS,
    SandboxLauncher,
    preflight,
)
from foreman.v4.subprocess_dispatcher import SubprocessRoleDispatcher


def _userns_available() -> bool:
    """True iff a minimal bwrap userns sandbox actually boots on this host.

    Uses the same OS exec-root mounts as the production sandbox
    (:data:`_OS_EXEC_ROOT_MOUNTS`). An earlier version bound ``/usr`` alone
    and ran ``/bin/true`` — on a usr-merged host the dynamic loader
    (``/lib64/ld-linux``) was then absent, so ``/bin/true`` failed to exec
    and this gate wrongly returned ``False``, silently self-skipping the
    ENTIRE integration module even where userns worked. That is exactly why
    these real-bwrap tests never caught the preflight exec-root drift.
    """
    if shutil.which("bwrap") is None:
        return False
    try:
        rc = subprocess.run(
            [
                "bwrap",
                "--unshare-user",
                *_OS_EXEC_ROOT_MOUNTS,
                "--tmpfs",
                "/tmp",
                "--",
                "/bin/true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
    except (OSError, subprocess.SubprocessError):
        return False
    return rc == 0


pytestmark = pytest.mark.skipif(
    not _userns_available(),
    reason="requires bwrap + unprivileged user namespaces",
)


def _run_in_box(
    role_cmd: list[str], *, cache_dir: Path, scratch_dir: Path
) -> subprocess.CompletedProcess[str]:
    launcher = SandboxLauncher(cache_dir=str(cache_dir))
    argv = launcher.build_argv(
        role_token="ghs_TESTTOKEN",
        scratch_dir=str(scratch_dir),
        role_cmd=role_cmd,
    )
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def test_preflight_boots_on_this_host() -> None:
    """The production ``preflight()`` must actually boot under real bwrap.

    Regression lock for the 2026-07-19 canary incident: ``preflight`` bound
    ``/usr`` alone and ran ``/bin/true``, which cannot find its ELF
    interpreter on a usr-merged image, so the daemon crash-looped
    fail-closed the moment the sandbox flag went live. The pure argv test
    (``test_preflight_argv_mounts_the_os_exec_roots``) guards the mount plan
    in CI; this exercises the real subprocess on any userns-capable host so
    the drift can never reach production unnoticed again."""
    preflight()  # raises SandboxUnavailableError on failure


def test_scratch_and_cache_are_both_writable(tmp_path: Path) -> None:
    """Mirrors the validated spike: scratch RW, cache RW (jobs warm it for later runs)."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "wheel.txt").write_text("cached", encoding="utf-8")
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    # scratch writable
    r = _run_in_box(
        ["/bin/sh", "-c", "echo hi > /scratch/out.txt && cat /scratch/out.txt"],
        cache_dir=cache_dir,
        scratch_dir=scratch_dir,
    )
    assert r.returncode == 0, r.stderr
    assert "hi" in r.stdout
    assert (scratch_dir / "out.txt").exists()  # write landed on the real host dir

    # cache read succeeds
    r = _run_in_box(
        ["/bin/sh", "-c", "cat /cache/wheel.txt"],
        cache_dir=cache_dir,
        scratch_dir=scratch_dir,
    )
    assert r.returncode == 0 and "cached" in r.stdout

    # cache write ALSO succeeds — the mount is read-write by design so
    # jobs can warm the shared, content-addressed uv cache for later runs.
    r = _run_in_box(
        ["/bin/sh", "-c", "echo warmed > /cache/new_wheel.txt && cat /cache/new_wheel.txt"],
        cache_dir=cache_dir,
        scratch_dir=scratch_dir,
    )
    assert r.returncode == 0, r.stderr
    assert "warmed" in r.stdout
    assert (cache_dir / "new_wheel.txt").exists()  # write landed on the real host dir


def test_daemon_secret_dir_is_invisible(tmp_path: Path) -> None:
    """A path we never bind (simulating /run/secrets, /root/.foreman) is absent."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    secret = tmp_path / "daemon_secret"
    secret.mkdir()
    (secret / "worker.pem").write_text("PRIVATE KEY", encoding="utf-8")

    r = _run_in_box(
        ["/bin/sh", "-c", f"cat {secret / 'worker.pem'} 2>&1 || echo INVISIBLE"],
        cache_dir=cache_dir,
        scratch_dir=scratch_dir,
    )
    assert "PRIVATE KEY" not in r.stdout
    assert "INVISIBLE" in r.stdout


def test_sandbox_identity_resolves_token_with_no_pem(tmp_path: Path) -> None:
    """In a real bwrap box with NO /run/secrets bound, the sandbox identity
    must still resolve a token from the injected GH_TOKEN. Regression lock for
    the 2026-07-19 canary, where the box tried to read
    /run/secrets/orchestrator_pem and crash-failed."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    launcher = SandboxLauncher(cache_dir=str(cache_dir))
    probe = (
        "import os;"
        "assert not os.path.exists('/run/secrets'), 'PEM dir leaked into box';"
        "from foreman.v4.identity import EnvTokenIdentity;"
        "t = EnvTokenIdentity().get_role_token('orchestrator');"
        "print('TOKEN_OK' if t == os.environ['GH_TOKEN'] else 'TOKEN_BAD')"
    )
    argv = launcher.build_argv(
        role_token="ghs_INJECTED_TESTTOKEN",
        scratch_dir=str(scratch_dir),
        role_cmd=["python", "-c", probe],
    )
    r = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert "TOKEN_OK" in r.stdout
    # the PEM path must never appear in a failure trace
    assert "orchestrator_pem" not in (r.stdout + r.stderr)
    assert "/run/secrets" not in r.stderr


def test_pid_isolation(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    r = _run_in_box(
        ["/bin/sh", "-c", "ls /proc | grep -c '^[0-9]*$'"],
        cache_dir=cache_dir,
        scratch_dir=scratch_dir,
    )
    assert r.returncode == 0
    # a fresh PID namespace shows only a handful of procs, never the host's hundreds
    assert int(r.stdout.strip()) < 20


def test_regression_2026_07_18_import_foreman_and_daemon_install_write_fail(
    tmp_path: Path,
) -> None:
    """PERMANENT lock for the 2026-07-18 foreman.prompts incident.

    From inside the box: (1) a write to the daemon's foreman install path
    fails because /usr is read-only and the daemon source is never mounted;
    (2) a fresh venv under /scratch cannot import foreman because the box's
    Python does not inherit the daemon's system site-packages on its path.

    Clarification (foreman#556): assertion (2) is specifically the FRESH
    ISOLATED venv context (``python3 -m venv`` with no system site-packages).
    That import failing is a real guarantee. It does NOT contradict the role
    entry point — which runs on the /usr-bound system Python and CAN import
    foreman. The isolation the sandbox actually provides is that foreman is
    READ-ONLY (assertion (1)): a job's ``uv sync`` cannot rewrite the
    daemon's install (the 2026-07-18 foreman.prompts corruption is
    structurally dead). See
    ``test_enabled_path_role_operates_in_private_clone`` below for the
    companion assertion that exercises this exact guarantee (a write to
    ``/usr`` fails) alongside the enabled path's private-clone isolation.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    # (1) writing into the RO system tree (where the daemon foreman lives) fails.
    r = _run_in_box(
        ["/bin/sh", "-c", "echo pwn > /usr/lib/foreman_marker.py"],
        cache_dir=cache_dir,
        scratch_dir=scratch_dir,
    )
    assert r.returncode != 0

    # (2) a clean venv under /scratch has no foreman on its path.
    r = _run_in_box(
        [
            "/bin/sh",
            "-c",
            "python3 -m venv /scratch/.venv "
            "&& /scratch/.venv/bin/python -c 'import foreman' "
            "&& echo IMPORTED || echo NO_FOREMAN",
        ],
        cache_dir=cache_dir,
        scratch_dir=scratch_dir,
    )
    assert "NO_FOREMAN" in r.stdout
    assert "IMPORTED" not in r.stdout


@pytest.mark.xfail(
    reason=(
        "stale since #556: drives dispatch() with the sandbox enabled but no "
        "sandbox_projects, so the dispatcher now raises 'no project map "
        "configured'. Hidden until the userns skip-gate was fixed. Tracked in "
        "foreman#562 — restore the project-map/clone-prep setup and drop this "
        "marker."
    ),
    strict=False,
)
def test_sandboxed_dispatch_log_banner_redacts_gh_token(tmp_path: Path) -> None:
    """Task-5 review gap: the on-disk log banner must never carry the raw token.

    Drives ``SubprocessRoleDispatcher`` end-to-end with a real bwrap box
    (not a mocked ``Popen``): the sandboxed child genuinely receives the
    real ``GH_TOKEN`` via ``bwrap --setenv`` (proven by the child echoing
    it back on stdout), while the banner ``_write_banner`` puts on disk
    carries only the redacted ``***`` placeholder for that same argv.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    log_dir = tmp_path / "logs"

    secret_token = "ghs_SUPERSECRETTOKEN"
    outcome_json = json.dumps({"kind": "clean", "confidence": "high", "summary": "ok"})
    script = f"echo \"TOKEN_SEEN:$GH_TOKEN\"; echo 'FOREMAN_OUTCOME:{outcome_json}'"

    identity = MagicMock()
    identity.get_role_token.return_value = secret_token

    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=["/bin/sh", "-c", script],
        identity=identity,
        log_dir=log_dir,
        sandbox=SandboxLauncher(cache_dir=str(cache_dir)),
        sandbox_scratch_root=scratch_root,
    )

    out = dispatcher.dispatch(role="worker", project="foreman", issue_number=555, ticket_id=1)

    assert "FOREMAN_OUTCOME:" in out
    # the box really did receive the real token — redaction is display-only
    assert f"TOKEN_SEEN:{secret_token}" in out

    log_files = list((log_dir / "worker").glob("*.log"))
    assert len(log_files) == 1
    banner = log_files[0].read_text(encoding="utf-8")
    assert secret_token not in banner
    assert "***" in banner


def test_enabled_path_role_operates_in_private_clone(tmp_path: Path) -> None:
    """Enabled path (real bwrap): prep a co-located private clone, bind it at the
    role's local_clone_path, and confirm a sandboxed command loads config,
    commits in the clone, sees the base repo as INVISIBLE, and cannot write foreman."""
    from foreman.v4.sandbox_clone import prepare_sandbox_clone

    # --- a real base git repo (stands in for /foreman/repos/<project>) ---
    base = tmp_path / "base"
    base.mkdir()
    subprocess.run(["git", "init", "-q", str(base)], check=True)
    subprocess.run(["git", "-C", str(base), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(base), "config", "user.name", "t"], check=True)
    (base / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(base), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(base), "commit", "-qm", "init"], check=True)

    # --- daemon preps the co-located private clone (same tmp fs → hardlinks) ---
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    job = tmp_path / ".scratch" / "proj" / "worker-1"
    clone_dir = job / "clone"
    wt_dir = job / "wt"
    wt_dir.mkdir(parents=True)
    prepare_sandbox_clone(
        base_clone_path=base,
        dest_clone_path=clone_dir,
        repo_url="https://github.com/o/n.git",  # never contacted in this test
        role_token="ghs_TESTTOKEN",
    )
    # config file the box will load
    config_file = tmp_path / "config.toml"
    config_file.write_text("log_dir = '/tmp'\n", encoding="utf-8")

    box_repo = "/foreman/repos/proj"
    launcher = SandboxLauncher(cache_dir=str(cache_dir))
    script = (
        # config file is readable at its RO mount
        f"cat {config_file} >/dev/null && "
        # operate in the private clone bound at the role's local_clone_path
        f"cd {box_repo} && git config user.email t@t.t && git config user.name t && "
        "echo work > f.txt && git add -A && git commit -qm work && git log --oneline | head -1 && "
        # the shared BASE repo path is NOT mounted → invisible
        f"(cat {base}/README.md 2>/dev/null && echo BASE_VISIBLE || echo BASE_INVISIBLE) && "
        # /usr (where the daemon foreman lives) is read-only
        "(echo x > /usr/lib/foreman_marker.py 2>/dev/null && echo FOREMAN_WRITABLE || echo FOREMAN_RO)"
    )
    argv = launcher.build_argv(
        role_token="ghs_TESTTOKEN",
        scratch_dir=str(wt_dir),
        role_cmd=["/bin/sh", "-c", script],
        repo_bind=(str(clone_dir), box_repo),
        ro_file_binds=((str(config_file), str(config_file)),),
    )
    r = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert "work" in r.stdout  # the commit landed in the private clone
    assert "BASE_INVISIBLE" in r.stdout
    assert "BASE_VISIBLE" not in r.stdout
    assert "FOREMAN_RO" in r.stdout
    assert "FOREMAN_WRITABLE" not in r.stdout


def test_two_role_handoff_box_sees_prior_role_commit(tmp_path: Path) -> None:
    """The clone-freshness keystone: role B's box, prepared after role A pushed,
    must see A's commit and diff it cleanly (reproduces #406's exit-128 against
    the pre-fix flow). Runs a real bwrap box; self-skips off userns.

    Correction vs. the originating brief's draft: the brief's snippet built
    the daemon's shared base clone with ``foreman.worktree.ensure_clone``
    (an ordinary working clone). Task 3 landed
    ``foreman.worktree.ensure_base_mirror`` instead — the base is now a
    **bare mirror** (no working tree, no stale local branches to leak into
    worktrees cut from it; see that function's docstring). This test builds
    the base with ``ensure_base_mirror`` to match the real daemon flow.

    Also, the private clone is bound into the box at a distinct in-box path
    (``/repo``, matching ``test_enabled_path_role_operates_in_private_clone``
    above) rather than reusing the host's ``tmp_path``-derived path as the
    brief's draft did — the host tmp dir normally lives under ``/tmp``, which
    ``build_argv`` already tmpfs-mounts for the box, so binding a second,
    unrelated source there at the identical path is avoidable path-collision
    risk with no benefit over a dedicated in-box path.
    """
    from foreman.v4 import sandbox_clone
    from foreman.worktree import ensure_base_mirror

    def git(*a: str, cwd: Path) -> str:
        return subprocess.run(
            ["git", *a], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, check=True, capture_output=True, text=True)

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True)
    git("config", "user.email", "t@t.t", cwd=seed)
    git("config", "user.name", "t", cwd=seed)
    (seed / "a.txt").write_text("a\n")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "init", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)

    base = tmp_path / "base"
    ensure_base_mirror(repo_url=str(origin), clone_path=base)  # bare mirror

    # role A pushes a new commit to origin
    (seed / "b.txt").write_text("b\n")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "role A", cwd=seed)
    new_sha = git("rev-parse", "HEAD", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)

    # role B's box, via the chokepoint (fetches origin)
    dest = tmp_path / "scratch" / "box"
    sandbox_clone.prepare_sandbox_clone(
        base_clone_path=base,
        dest_clone_path=dest,
        repo_url=str(origin),
        role_token="ghs_UNUSED",
        runner=runner,
    )

    # inside a REAL box, run the reviewer's diff shape against A's commit —
    # the private clone is bound RW at a dedicated in-box path (see docstring).
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    box_repo = "/repo"
    launcher = SandboxLauncher(cache_dir=str(cache_dir))
    argv = launcher.build_argv(
        role_token="ghs_UNUSED",
        scratch_dir=str(tmp_path / "scratch"),
        role_cmd=["git", "-C", box_repo, "diff", f"origin/main...{new_sha}"],
        repo_bind=(str(dest), box_repo),
    )
    r = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"box could not diff prior-role commit: {r.stderr[-400:]}"
