"""SubprocessRoleDispatcher — shells out to foreman <role> for v4 dispatch.

These tests drive a real Python subprocess as the stand-in "foreman
binary" so the streaming-log behavior (foreman#368) is exercised
against real pipes, not a mock. The dispatcher under test issues
``Popen([python, -c, script, ...])``; the script controls stdout /
stderr / timing / exit code.
"""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from foreman.v4.subprocess_dispatcher import (
    RoleSubprocessError,
    SubprocessRoleDispatcher,
    _base_role,
    _fs_safe_iso_utc,
)


def _stub_identity():
    """Builds a fake identity module exposing get_role_token."""
    mod = MagicMock()
    mod.get_role_token.return_value = "ghp_TESTTOKEN"
    return mod


def _python_script_cli(script: str) -> list[str]:
    """foreman_cli that runs the given Python snippet as the "binary".

    The dispatcher will append ``subcommand --project P --issue-number N
    [--target T]`` to this list. The snippet receives those via sys.argv
    and is free to ignore them; smoke tests use them only to confirm
    the dispatcher built the right command line.
    """
    return [sys.executable, "-c", script]


_HAPPY_OUTCOME = (
    'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}'
)


def _outcome_script(outcome_json: str = _HAPPY_OUTCOME, exit_code: int = 0) -> str:
    """A script that prints a couple of log lines then the outcome marker."""
    return textwrap.dedent(f"""
        import sys
        print("log line 1", flush=True)
        print("log line 2", flush=True)
        print({outcome_json!r}, flush=True)
        sys.exit({exit_code})
    """)


# ---------------------------------------------------------------------------
# Existing-behavior tests (rewritten on real subprocesses; pre-368 contract)
# ---------------------------------------------------------------------------


def test_planner_dispatch_returns_stdout_with_outcome(tmp_path: Path):
    """The state-machine contract: dispatch returns the subprocess's stdout
    so the verify hook can find the FOREMAN_OUTCOME: marker."""
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(_outcome_script()),
        identity=_stub_identity(),
        log_dir=tmp_path,
    )
    stdout = dispatcher.dispatch(
        role="planner", project="p", issue_number=1, ticket_id=1,
    )
    assert "FOREMAN_OUTCOME:" in stdout
    assert "log line 1" in stdout


@pytest.mark.parametrize(
    "role,subcmd,target",
    [
        ("planner", "plan", None),
        ("reviewer-spec", "review", "spec"),
        ("reviewer-impl", "review", "impl"),
        ("fixer-spec", "fix", "spec"),
        ("fixer-impl", "fix", "impl"),
        ("worker", "implement", None),
    ],
)
def test_role_to_subcommand_mapping(
    tmp_path: Path, role: str, subcmd: str, target: str | None,
):
    """The subprocess receives subcmd + --target on argv. Script echoes
    its argv to stderr so the test can assert command construction
    without mocking Popen."""
    script = textwrap.dedent(f"""
        import sys
        print('argv=' + repr(sys.argv), file=sys.stderr, flush=True)
        print({_HAPPY_OUTCOME!r}, flush=True)
    """)
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(script),
        identity=_stub_identity(),
        log_dir=tmp_path,
    )
    dispatcher.dispatch(role=role, project="p", issue_number=1, ticket_id=1)
    # The argv ends up in the log file as [stderr] lines.
    log_files = list((tmp_path / _base_role(role)).glob("*.log"))
    assert len(log_files) == 1
    log_text = log_files[0].read_text(encoding="utf-8")
    assert subcmd in log_text
    if target is not None:
        assert "--target" in log_text
        assert target in log_text


def test_subprocess_nonzero_with_error_outcome_returns_stdout(tmp_path: Path):
    """Non-zero exit + an emitted outcome → no exception at dispatcher
    layer. The state-machine verify hook decides what ERROR means."""
    script = _outcome_script(
        outcome_json='FOREMAN_OUTCOME:{"kind":"error","confidence":"high","summary":"boom"}',
        exit_code=1,
    )
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(script),
        identity=_stub_identity(),
        log_dir=tmp_path,
    )
    stdout = dispatcher.dispatch(
        role="planner", project="p", issue_number=1, ticket_id=1,
    )
    assert '"kind":"error"' in stdout


def test_subprocess_nonzero_without_outcome_raises(tmp_path: Path):
    """No marker on stdout + non-zero exit = hard error."""
    script = textwrap.dedent("""
        import sys
        print("killed", flush=True)
        sys.stderr.write("OOM\\n")
        sys.exit(137)
    """)
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(script),
        identity=_stub_identity(),
        log_dir=tmp_path,
    )
    with pytest.raises(RoleSubprocessError) as exc:
        dispatcher.dispatch(
            role="planner", project="p", issue_number=1, ticket_id=1,
        )
    assert "137" in str(exc.value)


def test_unknown_role_raises_value_error(tmp_path: Path):
    """Unknown role names raise ValueError BEFORE the subprocess spawns,
    so no log file is created."""
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(_outcome_script()),
        identity=_stub_identity(),
        log_dir=tmp_path,
    )
    with pytest.raises(ValueError, match="unknown role"):
        dispatcher.dispatch(
            role="not-a-role", project="p", issue_number=1, ticket_id=1,
        )
    # No log directory should have been created for an unknown role.
    assert list(tmp_path.glob("*/*.log")) == []


def test_identity_token_injected_per_role(tmp_path: Path):
    """get_role_token is called with the role name; the value lands in
    GH_TOKEN on the child's env. Child echoes GH_TOKEN to stdout."""
    identity = MagicMock()
    identity.get_role_token.side_effect = lambda r: f"token-for-{r}"
    script = textwrap.dedent(f"""
        import os
        print("GH_TOKEN=" + os.environ.get("GH_TOKEN", ""), flush=True)
        print({_HAPPY_OUTCOME!r}, flush=True)
    """)
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(script),
        identity=identity,
        log_dir=tmp_path,
    )
    stdout = dispatcher.dispatch(
        role="reviewer-spec", project="p", issue_number=1, ticket_id=1,
    )
    identity.get_role_token.assert_called_once_with("reviewer-spec")
    assert "GH_TOKEN=token-for-reviewer-spec" in stdout


def test_constructor_requires_log_dir():
    """log_dir is a REQUIRED kwarg (no default). Operators need somewhere
    to land role subprocess output on disk; refusing to construct without
    an explicit log_dir is the right failure mode."""
    with pytest.raises(TypeError):
        SubprocessRoleDispatcher(  # type: ignore[call-arg]
            foreman_cli=["foreman"], identity=_stub_identity(),
        )


# ---------------------------------------------------------------------------
# foreman#368 — live-stream behavior
# ---------------------------------------------------------------------------


def test_dispatch_writes_log_file_under_role_base_dir(tmp_path: Path):
    """Log file lands at <log_dir>/<role-base>/<ticket_id>__<iso>.log.

    Filename starts with the ticket id, contains the ISO timestamp with
    filesystem-safe separators, and ends with .log.
    """
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(_outcome_script()),
        identity=_stub_identity(),
        log_dir=tmp_path,
    )
    dispatcher.dispatch(
        role="planner", project="p", issue_number=42, ticket_id=99,
    )
    role_dir = tmp_path / "planner"
    assert role_dir.is_dir()
    logs = list(role_dir.glob("*.log"))
    assert len(logs) == 1
    name = logs[0].name
    assert name.startswith("99__")
    assert name.endswith(".log")
    # ISO shape: 99__YYYY-MM-DD-HH-MM-SS-mmmZ.log
    assert re.match(
        r"99__\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{3}Z\.log$", name,
    )


def test_dispatch_writes_start_banner_and_exit_code_footer(tmp_path: Path):
    """Log file starts with a banner naming role/ticket/project/issue
    and ends with --- exit code: N --- footer."""
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(_outcome_script()),
        identity=_stub_identity(),
        log_dir=tmp_path,
    )
    dispatcher.dispatch(
        role="planner", project="myproj", issue_number=42, ticket_id=99,
    )
    log_text = next((tmp_path / "planner").glob("*.log")).read_text(
        encoding="utf-8",
    )
    assert "--- role subprocess start ---" in log_text
    assert "role=planner" in log_text
    assert "ticket_id=99" in log_text
    assert "project=myproj" in log_text
    assert "issue_number=42" in log_text
    assert "--- exit code: 0 ---" in log_text


def test_dispatch_streams_stdout_to_log_file_with_flush(tmp_path: Path):
    """The load-bearing test: a concurrent reader sees stdout lines on
    disk WHILE the subprocess is still running. If the implementation
    buffers stdout until exit, this test hangs (or sees an empty file
    at the polling boundary and times out)."""
    # Subprocess prints 3 lines with 200ms gaps between them, then sleeps
    # another 500ms so the reader has a generous window to observe a
    # mid-run line before exit.
    script = textwrap.dedent(f"""
        import sys, time
        print("line-1", flush=True)
        time.sleep(0.2)
        print("line-2", flush=True)
        time.sleep(0.2)
        print("line-3", flush=True)
        time.sleep(0.5)
        print({_HAPPY_OUTCOME!r}, flush=True)
    """)
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(script),
        identity=_stub_identity(),
        log_dir=tmp_path,
    )

    saw_line_2_mid_run = threading.Event()

    def poll_for_line():
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            role_dir = tmp_path / "planner"
            if role_dir.is_dir():
                for log in role_dir.glob("*.log"):
                    text = log.read_text(encoding="utf-8", errors="replace")
                    if "line-2" in text and "--- exit code:" not in text:
                        saw_line_2_mid_run.set()
                        return
            time.sleep(0.05)

    poller = threading.Thread(target=poll_for_line, daemon=True)
    poller.start()
    dispatcher.dispatch(
        role="planner", project="p", issue_number=1, ticket_id=7,
    )
    poller.join(timeout=1.0)

    assert saw_line_2_mid_run.is_set(), (
        "concurrent reader did not see line-2 on disk before the subprocess "
        "exited — implementation likely buffers output instead of flushing"
    )


def test_dispatch_streams_stderr_with_prefix(tmp_path: Path):
    """Stderr lines land in the merged log file with a ``[stderr] `` prefix
    so an operator scanning the file can tell them apart from stdout."""
    script = textwrap.dedent(f"""
        import sys
        print("stdout-A", flush=True)
        print("stderr-B", file=sys.stderr, flush=True)
        print("stdout-C", flush=True)
        print({_HAPPY_OUTCOME!r}, flush=True)
    """)
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(script),
        identity=_stub_identity(),
        log_dir=tmp_path,
    )
    dispatcher.dispatch(
        role="planner", project="p", issue_number=1, ticket_id=1,
    )
    log_text = next((tmp_path / "planner").glob("*.log")).read_text(
        encoding="utf-8",
    )
    assert "[stderr] stderr-B" in log_text
    # Stdout lines must NOT carry the stderr prefix.
    assert "[stderr] stdout-A" not in log_text
    assert "[stderr] stdout-C" not in log_text


def test_dispatch_timeout_writes_marker_and_raises(tmp_path: Path):
    """When the subprocess exceeds its timeout: kill it, write the
    TIMEOUT marker to the log file (partial output preserved), and raise
    RoleSubprocessError."""
    # Script prints something then sleeps forever (well, 30s).
    script = textwrap.dedent("""
        import time
        print("starting", flush=True)
        time.sleep(30)
    """)
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(script),
        identity=_stub_identity(),
        log_dir=tmp_path,
        timeout_seconds=1,
    )
    with pytest.raises(RoleSubprocessError) as exc:
        dispatcher.dispatch(
            role="planner", project="p", issue_number=1, ticket_id=1,
        )
    assert "timeout" in str(exc.value).lower()
    log_text = next((tmp_path / "planner").glob("*.log")).read_text(
        encoding="utf-8",
    )
    assert "starting" in log_text  # partial output preserved
    assert "--- TIMEOUT after 1s ---" in log_text


def test_dispatch_target_aware_role_lands_in_base_role_dir(tmp_path: Path):
    """``reviewer-spec`` and ``reviewer-impl`` both write into
    ``<log_dir>/reviewer/``, not separate subdirs. Same for fixer."""
    for role in ("reviewer-spec", "reviewer-impl"):
        SubprocessRoleDispatcher(
            foreman_cli=_python_script_cli(_outcome_script()),
            identity=_stub_identity(),
            log_dir=tmp_path,
        ).dispatch(role=role, project="p", issue_number=1, ticket_id=1)
    assert (tmp_path / "reviewer").is_dir()
    assert not (tmp_path / "reviewer-spec").exists()
    assert not (tmp_path / "reviewer-impl").exists()
    # Both runs left their own log file under reviewer/.
    assert len(list((tmp_path / "reviewer").glob("*.log"))) == 2


def test_dispatch_handles_concurrent_stdout_and_stderr_no_deadlock(
    tmp_path: Path,
):
    """Both streams burst 50+ lines quickly. With a single-thread reader
    the OS pipe buffer for the non-drained stream would fill and deadlock
    the writer. Two threads = no deadlock; test must complete < timeout."""
    script = textwrap.dedent(f"""
        import sys
        for i in range(80):
            print("out-" + str(i), flush=True)
            print("err-" + str(i), file=sys.stderr, flush=True)
        print({_HAPPY_OUTCOME!r}, flush=True)
    """)
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(script),
        identity=_stub_identity(),
        log_dir=tmp_path,
        timeout_seconds=10,
    )
    stdout = dispatcher.dispatch(
        role="planner", project="p", issue_number=1, ticket_id=1,
    )
    # 80 stdout lines plus the outcome line all captured.
    assert stdout.count("out-") == 80
    log_text = next((tmp_path / "planner").glob("*.log")).read_text(
        encoding="utf-8",
    )
    assert log_text.count("[stderr] err-") == 80


def test_dispatch_aborted_marker_on_unexpected_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """If a non-timeout exception fires mid-dispatch (e.g., Popen raises
    OSError because the binary is gone), the log file closes cleanly
    with an ABORTED marker AND the original exception propagates."""
    real_popen = subprocess.Popen
    call_count = {"n": 0}

    def flaky_popen(*args, **kwargs):
        call_count["n"] += 1
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(subprocess, "Popen", flaky_popen)
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(_outcome_script()),
        identity=_stub_identity(),
        log_dir=tmp_path,
    )
    with pytest.raises(OSError, match="simulated spawn failure"):
        dispatcher.dispatch(
            role="planner", project="p", issue_number=1, ticket_id=1,
        )
    # Log file exists with the banner + ABORTED marker, no exit-code
    # footer (because the subprocess never ran).
    logs = list((tmp_path / "planner").glob("*.log"))
    assert len(logs) == 1
    log_text = logs[0].read_text(encoding="utf-8")
    assert "--- role subprocess start ---" in log_text
    assert "--- ABORTED ---" in log_text
    assert "--- exit code:" not in log_text
    # Sanity: real_popen still exists so other tests aren't poisoned.
    assert real_popen is not None


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,expected",
    [
        ("planner", "planner"),
        ("worker", "worker"),
        ("reviewer-spec", "reviewer"),
        ("reviewer-impl", "reviewer"),
        ("fixer-spec", "fixer"),
        ("fixer-impl", "fixer"),
    ],
)
def test_base_role_strips_target_suffix(role: str, expected: str):
    assert _base_role(role) == expected


def test_fs_safe_iso_utc_replaces_colons_and_dot_and_t_and_z():
    import datetime as dt

    now = dt.datetime(2026, 6, 19, 23, 30, 12, 456000, tzinfo=dt.UTC)
    iso = _fs_safe_iso_utc(now)
    assert iso == "2026-06-19-23-30-12-456Z"
    # Filesystem-illegal characters on Windows must not appear.
    for char in (":", "/", "\\", "*", "?", "\"", "<", ">", "|"):
        assert char not in iso
