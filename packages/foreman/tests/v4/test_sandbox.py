"""Pure unit tests for SandboxLauncher.build_argv — runs on every platform."""

from __future__ import annotations

from foreman.v4.sandbox import DAEMON_NEVER_BIND, SandboxLauncher


def _argv() -> list[str]:
    launcher = SandboxLauncher(cache_dir="/root/.cache/uv")
    return launcher.build_argv(
        role_token="ghs_SECRET",
        scratch_dir="/foreman/scratch/foreman/worker-537",
        role_cmd=["foreman", "implement", "--project", "foreman", "--issue-number", "537"],
    )


def test_argv_wraps_role_cmd_after_double_dash() -> None:
    argv = _argv()
    assert argv[0] == "bwrap"
    dd = argv.index("--")
    assert argv[dd + 1 :] == [
        "foreman",
        "implement",
        "--project",
        "foreman",
        "--issue-number",
        "537",
    ]


def test_argv_has_the_canon_namespace_and_lifecycle_flags() -> None:
    argv = _argv()
    for flag in (
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--share-net",
        "--die-with-parent",
    ):
        assert flag in argv, flag
    # tmpfs /tmp
    assert _has_pair(argv, "--tmpfs", "/tmp")


def test_argv_mounts_cache_rw_and_scratch_rw() -> None:
    argv = _argv()
    # RW cache: --bind <cache_dir> /cache (writable so jobs warm the shared cache)
    assert _has_triple(argv, "--bind", "/root/.cache/uv", "/cache")
    # cache must NOT be mounted read-only (would force re-download of new deps/projects)
    assert not _has_triple(argv, "--ro-bind", "/root/.cache/uv", "/cache")
    # RW scratch: --bind <scratch_dir> /scratch (writable bind, NOT ro-bind)
    assert _has_triple(argv, "--bind", "/foreman/scratch/foreman/worker-537", "/scratch")
    # scratch must never be mounted read-only
    assert not _has_triple(argv, "--ro-bind", "/foreman/scratch/foreman/worker-537", "/scratch")


def test_argv_mounts_base_ro_roots() -> None:
    argv = _argv()
    assert _has_triple(argv, "--ro-bind", "/usr", "/usr")
    assert _has_triple(argv, "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf")
    assert _has_triple(argv, "--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs")


def test_argv_sets_scoped_token_and_scratch_rooted_env() -> None:
    argv = _argv()
    assert _has_triple(argv, "--setenv", "GH_TOKEN", "ghs_SECRET")
    assert _has_triple(argv, "--setenv", "FOREMAN_WORKTREES_ROOT", "/scratch")
    assert _has_triple(argv, "--setenv", "UV_CACHE_DIR", "/cache")
    # the box starts from a cleared env so no daemon secret leaks in
    assert "--clearenv" in argv


def test_argv_never_binds_daemon_secrets() -> None:
    argv = _argv()
    joined = " ".join(argv)
    for forbidden in DAEMON_NEVER_BIND:
        assert forbidden not in joined, forbidden
    # explicit PEM paths
    for pem in ("planner_pem", "reviewer_pem", "fixer_pem", "worker_pem", "orchestrator_pem"):
        assert pem not in joined, pem


def test_argv_passthrough_forwards_non_secret_env_only() -> None:
    launcher = SandboxLauncher(cache_dir="/root/.cache/uv")
    argv = launcher.build_argv(
        role_token="ghs_X",
        scratch_dir="/foreman/scratch/foreman/planner-42",
        role_cmd=["foreman", "plan"],
        passthrough={
            "FOREMAN_STATE_INSTANCE_ID": "9",
            "CLAUDE_CONFIG_DIR": "/root/.claude-container",
        },
    )
    assert _has_triple(argv, "--setenv", "FOREMAN_STATE_INSTANCE_ID", "9")
    assert _has_triple(argv, "--setenv", "CLAUDE_CONFIG_DIR", "/root/.claude-container")


def _has_pair(argv: list[str], a: str, b: str) -> bool:
    return any(argv[i] == a and argv[i + 1] == b for i in range(len(argv) - 1))


def _has_triple(argv: list[str], a: str, b: str, c: str) -> bool:
    return any(argv[i] == a and argv[i + 1] == b and argv[i + 2] == c for i in range(len(argv) - 2))
