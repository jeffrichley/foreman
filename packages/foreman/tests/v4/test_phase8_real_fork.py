"""Real-fork integration test — invokes the actual installed foreman
binary as a subprocess and verifies the full chain works.

Phase 5.7 used a stub Python script that pretended to be ``foreman``.
Phase 7.6 monkey-patched the dispatcher to avoid forking at all. This
test is qualitatively different: it actually forks ``python -m
foreman.v4.cli <role-subcommand>`` and verifies the typer command +
``main()`` bootstrap entry + role CLI entry + ``emit_outcome`` + the
``parse_outcome_from_stdout`` parser all chain together against the
real installed binary, not a stub or a mocked seam.

Two acceptance angles are covered:

1. ``test_role_subcommand_real_fork_emits_parseable_outcome`` — the
   role's real work (provider call, PyGithub, worktree) is short-
   circuited via ``FOREMAN_DRY_RUN=1`` (the spec's explicit fallback
   at v4-phase-8 lines 158-160). That flag also short-circuits
   ``main()``'s bootstrap so the test doesn't need real App
   credentials. Proves the typer → role-entry → emit_outcome chain
   works under the real entry-point binary.
2. ``test_planner_real_fork_loads_v4_config_without_v3_config_present`` —
   Phase 8b.3 added the v4-config-load path to ``_run_planner_for_v4``.
   Without ``FOREMAN_DRY_RUN`` the planner CLI now reaches ``v4_load_config``,
   so this case proves the new v4-config-load seam works end-to-end
   without needing a v3 ``~/.foreman/config.toml`` on disk. The
   subprocess is expected to exit non-zero downstream (no real PEM key
   present), but the failure must come from PyGithub / token-mint, not
   from a config-load shaped crash.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout

# Subprocess startup on Windows / cold-import of the typer app costs
# more than a few hundred ms; cap each invocation generously so a true
# hang fails fast rather than hanging CI.
_SUBPROCESS_TIMEOUT_SECONDS = 30


def _build_dry_run_env() -> dict[str, str]:
    """Minimal env for the dry-run subprocess.

    ``FOREMAN_DRY_RUN=1`` short-circuits both ``main()`` (skips
    bootstrap / config-load / identity construction) and the role CLI
    entry-points (emits a canned CLEAN outcome before any real work).
    PATH + PYTHONPATH pass through so the subprocess can resolve shared
    libs and the editable foreman install. SystemRoot is required on
    Windows for the Python interpreter to start, and USERPROFILE /
    HOMEDRIVE+HOMEPATH / HOME are required for ``Path.home()`` — which
    ``foreman.v4.cli.daemon`` evaluates at import time for the PID-file
    default, even when the daemon commands themselves are never run.
    """
    env: dict[str, str] = {"FOREMAN_DRY_RUN": "1"}
    for key in (
        "PATH",
        "PYTHONPATH",
        "SystemRoot",
        "SYSTEMROOT",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "HOME",
    ):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


@pytest.mark.parametrize(
    "subcommand,extra_args",
    [
        ("plan", []),
        ("review", ["--target", "spec"]),
        ("fix", ["--target", "spec"]),
        ("implement", []),
    ],
    ids=["plan", "review-spec", "fix-spec", "implement"],
)
def test_role_subcommand_real_fork_emits_parseable_outcome(
    subcommand: str, extra_args: list[str], tmp_path: Path,
) -> None:
    """Each role subcommand, invoked as a real subprocess under
    ``FOREMAN_DRY_RUN=1``, emits a parseable CLEAN outcome and exits 0.
    """
    cmd = [
        sys.executable, "-m", "foreman.v4.cli", subcommand,
        "--project", "p", "--issue-number", "1",
        *extra_args,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_build_dry_run_env(),
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        cwd=tmp_path,
    )
    assert result.returncode == 0, (
        f"foreman {subcommand} exited non-zero: rc={result.returncode}\n"
        f"cmd: {cmd}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    outcome = parse_outcome_from_stdout(result.stdout)
    assert outcome.kind == OutcomeKind.CLEAN, (
        f"expected CLEAN outcome for dry-run, got {outcome.kind!r}\n"
        f"stdout:\n{result.stdout}"
    )
    assert outcome.summary == "dry-run", (
        f"expected dry-run summary, got {outcome.summary!r}"
    )


def _build_v4_only_env(*, v4_cfg_path: Path) -> dict[str, str]:
    """Env for a real fork that exercises the v4-config-load seam.

    Sets ``FOREMAN_V4_CONFIG`` (so the role CLI finds the v4 TOML the
    test wrote) and DELIBERATELY OMITS ``FOREMAN_DRY_RUN``, ``FOREMAN_CONFIG``,
    and ``FOREMAN_CONFIG_PATH``. The legacy v3-config-load path inside
    ``_run_<role>_for_v4`` (Phase 8b.3 removed it) would have read those
    env vars and fallen back to ``~/.foreman/config.toml`` — proving its
    absence requires forcing the subprocess to use ONLY the v4 path.

    Same Windows-bring-up keys as ``_build_dry_run_env`` so
    ``Path.home()`` resolves and the interpreter starts.
    """
    env: dict[str, str] = {"FOREMAN_V4_CONFIG": str(v4_cfg_path)}
    for key in (
        "PATH",
        "PYTHONPATH",
        "SystemRoot",
        "SYSTEMROOT",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "HOME",
    ):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


def _write_v4_config_toml(*, cfg_path: Path, project_name: str, repo: str) -> None:
    """Write a minimum-valid V4Config TOML at ``cfg_path``.

    Uses fake App credentials — the test deliberately expects the
    downstream PyGithub token-mint to fail, because the test exists to
    prove the v4-config-load seam works *before* the token-mint runs.
    """
    db_path = cfg_path.parent / "v4.db"
    log_dir = cfg_path.parent / "logs"
    cfg_path.write_text(
        f"""\
[daemon]
db_path = "{db_path.as_posix()}"
log_dir = "{log_dir.as_posix()}"

[apps.planner]
app_id = 12345
private_key_path = "/tmp/does-not-exist-planner.pem"

[apps.reviewer]
app_id = 12346
private_key_path = "/tmp/does-not-exist-reviewer.pem"

[apps.fixer]
app_id = 12347
private_key_path = "/tmp/does-not-exist-fixer.pem"

[apps.worker]
app_id = 12348
private_key_path = "/tmp/does-not-exist-worker.pem"

[orchestrator]
app_id = 99999
private_key_path = "/tmp/does-not-exist-orch.pem"

[operator.supervisor]
name = "Test Sup"
email = "sup@example.com"

[operator.signer]
name = "Test Sign"
email = "sign@example.com"

[[projects]]
name = "{project_name}"
repo = "{repo}"
local_clone_path = "/tmp/does-not-exist-clone"
""",
        encoding="utf-8",
    )


def test_planner_real_fork_loads_v4_config_without_v3_config_present(
    tmp_path: Path,
) -> None:
    """Planner subprocess parses V4Config without a v3 ``~/.foreman/config.toml``.

    Phase 8b.3 rewired ``_run_planner_for_v4`` from v3 ``load_config``
    to ``v4_load_config``. Pre-rewire the subprocess would have crashed
    inside ``_run_planner_for_v4`` with ``KeyError: 'algokit'`` (since
    the v3 config didn't carry the project) or ``FileNotFoundError``
    on ``~/.foreman/config.toml``.

    Post-rewire the subprocess reaches v4 ``main()``'s identity-bootstrap
    BEFORE the role CLI body runs — and that bootstrap calls
    ``mint_installation_token`` which needs a real PEM on disk. The
    fake ``/tmp/does-not-exist-*.pem`` paths in the test TOML mean the
    subprocess crashes there, NOT in the role CLI body. That crash
    proves two things in one shot:

    1. The V4Config TOML parsed cleanly (otherwise the crash would be a
       pydantic ValidationError BEFORE identity-bootstrap).
    2. The crash signature is a PEM-file path, not a v3 config-toml
       path. The v3 path is gone.

    The subprocess exits non-zero with a traceback on stderr; this test
    asserts the traceback mentions the test's fake PEM path (so we
    know v4 config-load got far enough to thread credentials into
    identity-bootstrap) AND does NOT mention v3 config paths.
    """
    cfg_path = tmp_path / "v4-config.toml"
    _write_v4_config_toml(
        cfg_path=cfg_path, project_name="algokit", repo="jeffrichley/algokit",
    )

    cmd = [
        sys.executable, "-m", "foreman.v4.cli", "plan",
        "--project", "algokit", "--issue-number", "1",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_build_v4_only_env(v4_cfg_path=cfg_path),
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        cwd=tmp_path,
    )

    assert result.returncode != 0, (
        f"expected non-zero exit (fake PEM credentials cannot mint a real "
        f"token); got rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    combined = (result.stdout + "\n" + result.stderr).lower()

    # Positive assert: the failure is from threading the v4-loaded
    # credentials into identity-bootstrap (proving V4Config parsed
    # cleanly AND its values reached the boundary). The fake PEM path
    # was set in the TOML the test wrote.
    assert "does-not-exist" in combined, (
        f"expected the failure to reference a fake PEM path from the v4 TOML "
        f"(proving the v4-config-load path threaded credentials); got\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # Negative assert: the pre-Phase-8b.3 failure mode would mention
    # the v3 ``~/.foreman/config.toml`` path or a ``KeyError`` from the
    # v3-projects-dict lookup. Those signatures are gone.
    forbidden_substrings = [
        ".foreman/config.toml",
        ".foreman\\config.toml",
    ]
    for needle in forbidden_substrings:
        assert needle not in combined, (
            f"output still mentions v3 config path {needle!r}; the rewire is "
            f"incomplete.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
