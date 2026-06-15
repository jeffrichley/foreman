"""Real-fork integration test — invokes the actual installed foreman
binary as a subprocess and verifies the full chain works.

Phase 5.7 used a stub Python script that pretended to be ``foreman``.
Phase 7.6 monkey-patched the dispatcher to avoid forking at all. This
test is qualitatively different: it actually forks ``python -m
foreman.v4.cli <role-subcommand>`` and verifies the typer command +
``main()`` bootstrap entry + role CLI entry + ``emit_outcome`` + the
``parse_outcome_from_stdout`` parser all chain together against the
real installed binary, not a stub or a mocked seam.

The role's real work (provider call, PyGithub, worktree) is the only
piece short-circuited — via ``FOREMAN_DRY_RUN=1`` (the spec's explicit
fallback at v4-phase-8 lines 158-160). That flag also short-circuits
``main()``'s bootstrap path so the test doesn't need PyGithub or any
real App credentials. What's exercised end-to-end is the chain that
``SubprocessRoleDispatcher`` actually relies on at runtime, which no
other v4 test covers against the real entry-point binary.

Acceptance per spec:
- ``subprocess.run`` invokes the real ``python -m foreman.v4.cli`` path
- ``FOREMAN_OUTCOME`` is parseable from stdout
- exit code is 0 (clean dry-run outcome)
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
