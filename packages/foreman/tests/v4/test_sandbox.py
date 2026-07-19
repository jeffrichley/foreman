"""Pure unit tests for SandboxLauncher.build_argv and preflight — runs on every platform."""

from __future__ import annotations

import pytest

from foreman.v4.sandbox import (
    DAEMON_NEVER_BIND,
    SandboxLauncher,
    SandboxUnavailableError,
    preflight,
)


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


def test_argv_binds_private_repo_rw_and_config_files_ro() -> None:
    launcher = SandboxLauncher(cache_dir="/root/.cache/uv")
    argv = launcher.build_argv(
        role_token="ghs_SECRET",
        scratch_dir="/foreman/repos/.scratch/foreman/worker-537/wt",
        role_cmd=["foreman", "implement", "--project", "foreman"],
        repo_bind=(
            "/foreman/repos/.scratch/foreman/worker-537/clone",
            "/foreman/repos/foreman",
        ),
        ro_file_binds=(
            ("/foreman/state/config.toml", "/foreman/state/config.toml"),
            ("/root/.foreman/projects.toml", "/root/.foreman/projects.toml"),
        ),
    )
    # private clone is RW-bound at the in-box repo path the role's config names
    assert _has_triple(
        argv,
        "--bind",
        "/foreman/repos/.scratch/foreman/worker-537/clone",
        "/foreman/repos/foreman",
    )
    # it is NOT read-only — the role commits into it
    assert not _has_triple(
        argv,
        "--ro-bind",
        "/foreman/repos/.scratch/foreman/worker-537/clone",
        "/foreman/repos/foreman",
    )
    # config + projects files are RO-bound at their expected paths
    assert _has_triple(
        argv, "--ro-bind", "/foreman/state/config.toml", "/foreman/state/config.toml"
    )
    assert _has_triple(
        argv, "--ro-bind", "/root/.foreman/projects.toml", "/root/.foreman/projects.toml"
    )


def test_argv_omits_repo_and_file_binds_when_not_requested() -> None:
    """Back-compat: existing callers that pass neither get the pre-#556 argv shape."""
    launcher = SandboxLauncher(cache_dir="/root/.cache/uv")
    argv = launcher.build_argv(
        role_token="ghs_X",
        scratch_dir="/scratch",
        role_cmd=["foreman", "plan"],
    )
    # no stray config bind, no RW bind other than cache + scratch
    assert argv.count("--bind") == 2  # cache + scratch only


def test_argv_never_binds_daemon_secrets_with_repo_and_file_binds_set() -> None:
    """The #556 repo/config binds must not regress the never-bind invariant.

    ``ro_file_binds`` here intentionally includes ``projects.toml``, which
    lives under the never-bind ``/root/.foreman`` prefix (see
    :data:`DAEMON_NEVER_BIND`'s comment: that directory holds "the
    credential vault, projects.toml, keys/, backups"). Binding that single
    *file* read-only does not expose its siblings (the vault, keys/,
    backups) since bwrap file binds mount only the named node, not its
    parent directory. So the check below asserts each never-bind path is
    absent as a whole ``argv`` element (never a bind source/target
    itself) rather than absent as a substring — a plain substring check
    would false-positive on the allow-listed file living under that
    prefix, which is exactly why ``ro_file_binds`` is a separate, narrow
    escape hatch instead of ever bulk-mounting ``/root/.foreman``.
    """
    launcher = SandboxLauncher(cache_dir="/root/.cache/uv")
    argv = launcher.build_argv(
        role_token="ghs_SECRET",
        scratch_dir="/foreman/repos/.scratch/foreman/worker-537/wt",
        role_cmd=["foreman", "implement", "--project", "foreman"],
        repo_bind=(
            "/foreman/repos/.scratch/foreman/worker-537/clone",
            "/foreman/repos/foreman",
        ),
        ro_file_binds=(
            ("/foreman/state/config.toml", "/foreman/state/config.toml"),
            ("/root/.foreman/projects.toml", "/root/.foreman/projects.toml"),
        ),
    )
    # never-bind DIRECTORIES themselves must never appear as a bind arg —
    # only the single allow-listed projects.toml FILE beneath one of them.
    for forbidden in DAEMON_NEVER_BIND:
        assert forbidden not in argv, forbidden
    # explicit PEM paths never leak in either
    joined = " ".join(argv)
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


def test_argv_adds_writable_claude_session_tmpfs_when_set() -> None:
    launcher = SandboxLauncher(
        cache_dir="/root/.cache/uv",
        claude_writable_session_dir="/root/.claude/projects",
    )
    argv = launcher.build_argv(
        role_token="ghs_X", scratch_dir="/scratch", role_cmd=["foreman", "plan"]
    )
    assert _has_pair(argv, "--tmpfs", "/root/.claude/projects")
    # the tmpfs must come AFTER the RO creds bind so it overlays (writable) it
    ro_i = argv.index("/root/.claude")  # from extra_ro_binds
    tmp_i = argv.index("/root/.claude/projects")
    assert tmp_i > ro_i
    # the RO creds bind itself is unchanged: still a --ro-bind-try triple,
    # not upgraded to a writable --bind.
    assert _has_triple(argv, "--ro-bind-try", "/root/.claude", "/root/.claude")
    assert not _has_triple(argv, "--bind", "/root/.claude", "/root/.claude")
    # the never-bind guardrail still holds with the session tmpfs configured
    joined = " ".join(argv)
    for forbidden in DAEMON_NEVER_BIND:
        assert forbidden not in joined, forbidden


def test_argv_no_claude_tmpfs_by_default() -> None:
    argv = SandboxLauncher(cache_dir="/c").build_argv(
        role_token="ghs_X", scratch_dir="/scratch", role_cmd=["foreman", "plan"]
    )
    assert not _has_pair(argv, "--tmpfs", "/root/.claude/projects")


def _has_pair(argv: list[str], a: str, b: str) -> bool:
    return any(argv[i] == a and argv[i + 1] == b for i in range(len(argv) - 1))


def _has_triple(argv: list[str], a: str, b: str, c: str) -> bool:
    return any(argv[i] == a and argv[i + 1] == b and argv[i + 2] == c for i in range(len(argv) - 2))


def test_preflight_passes_when_runner_returns_zero() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, ""

    preflight(bwrap_path="bwrap", runner=runner)  # no raise
    assert calls, "preflight must actually invoke bwrap"
    assert calls[0][0] == "bwrap"
    assert "--unshare-user" in calls[0]


def test_preflight_fails_closed_when_runner_returns_nonzero() -> None:
    distinctive_stderr = "bwrap: setting up uid map: Permission denied"

    def runner(argv: list[str]) -> tuple[int, str]:
        return 1, distinctive_stderr

    with pytest.raises(SandboxUnavailableError) as exc:
        preflight(bwrap_path="bwrap", runner=runner)
    message = str(exc.value)
    assert "user namespace" in message.lower() or "bwrap" in message.lower()
    assert distinctive_stderr in message


def test_preflight_fails_closed_when_bwrap_missing() -> None:
    def runner(argv: list[str]) -> tuple[int, str]:
        raise FileNotFoundError("bwrap")

    with pytest.raises(SandboxUnavailableError):
        preflight(bwrap_path="bwrap", runner=runner)


def test_preflight_fails_closed_on_permission_error() -> None:
    def runner(argv: list[str]) -> tuple[int, str]:
        raise PermissionError("bwrap")

    with pytest.raises(SandboxUnavailableError):
        preflight(bwrap_path="bwrap", runner=runner)
