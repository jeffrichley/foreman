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

from foreman.v4.sandbox import SandboxLauncher
from foreman.v4.subprocess_dispatcher import SubprocessRoleDispatcher


def _userns_available() -> bool:
    """True iff a minimal bwrap userns sandbox actually boots on this host."""
    if shutil.which("bwrap") is None:
        return False
    try:
        rc = subprocess.run(
            [
                "bwrap",
                "--unshare-user",
                "--ro-bind",
                "/usr",
                "/usr",
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
