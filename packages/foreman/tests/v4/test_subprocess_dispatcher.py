"""SubprocessRoleDispatcher — shells out to foreman <role> for v4 dispatch.

These tests drive a real Python subprocess as the stand-in "foreman
binary" so the streaming-log behavior (foreman#368) is exercised
against real pipes, not a mock. The dispatcher under test issues
``Popen([python, -c, script, ...])``; the script controls stdout /
stderr / timing / exit code.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from foreman.v4 import subprocess_dispatcher as sd
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
    # I5: file handle must be released so Windows allows unlink.
    logs[0].unlink()
    assert not logs[0].exists()
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


# ---------------------------------------------------------------------------
# Code-quality fix pass: exception-path resource lifecycle (C1+C2+C3),
# UTF-8 encoding (I1), error-message log path (I4), bounded joins (I6).
# ---------------------------------------------------------------------------


def test_dispatch_reaps_subprocess_when_internal_exception_raised_mid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """If something raises AFTER Popen succeeds — e.g. an internal helper
    blows up — the subprocess must be killed + reaped and the log file
    handle released. Otherwise we leak children and Windows file handles
    until daemon restart.

    We capture the spawned ``Popen`` instance and patch ``threading.Thread``
    to raise; the dispatcher's cleanup must kill+reap the subprocess
    before propagating the exception. After dispatch raises, ``proc.poll()``
    must return a non-None exit code (subprocess reaped, not still running).
    """
    # Script sleeps long enough that the subprocess is definitely still
    # running when we synthesize the failure.
    script = textwrap.dedent("""
        import time
        print("started", flush=True)
        time.sleep(10)
    """)
    spawned_procs: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def tracking_popen(*args, **kwargs):
        p = real_popen(*args, **kwargs)
        spawned_procs.append(p)
        return p

    monkeypatch.setattr(sd.subprocess, "Popen", tracking_popen)

    real_thread = threading.Thread

    def boom_thread(*args, **kwargs):
        raise RuntimeError("synthetic mid-run failure")

    monkeypatch.setattr(sd.threading, "Thread", boom_thread)

    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(script),
        identity=_stub_identity(),
        log_dir=tmp_path,
        timeout_seconds=30,
    )
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="synthetic mid-run failure"):
        dispatcher.dispatch(
            role="planner", project="p", issue_number=1, ticket_id=1,
        )
    elapsed = time.monotonic() - start
    # Must NOT hang 10s waiting for the subprocess. Reaping should be fast.
    assert elapsed < 8.0, (
        f"dispatch hung {elapsed:.1f}s after mid-run exception; "
        "subprocess likely not reaped"
    )

    # The subprocess we spawned must be reaped — poll() returns the
    # exit code (or signal), not None.
    assert len(spawned_procs) == 1
    proc = spawned_procs[0]
    assert proc.poll() is not None, (
        "subprocess leaked: still running after dispatch raised mid-run"
    )

    # ABORTED marker present (this is a non-timeout failure path).
    logs = list((tmp_path / "planner").glob("*.log"))
    assert len(logs) == 1
    log_text = logs[0].read_text(encoding="utf-8")
    assert "--- ABORTED ---" in log_text
    # Log file handle must be released — Windows blocks unlink otherwise.
    logs[0].unlink()

    # Real refs restored after monkeypatch teardown — sanity.
    assert real_thread is not None
    assert real_popen is not None


def test_dispatch_joins_reader_threads_in_aborted_path(
    tmp_path: Path,
):
    """Reader threads must be joined before dispatch returns/raises.

    We track reader threads by name pattern. If they're still alive
    after dispatch raises, we leaked them (the old code did this on
    every non-happy exit because joins lived in the happy branch only).
    """
    # Subprocess emits a steady stream then exits 1 without a marker so
    # dispatch raises RoleSubprocessError on the happy-path exit too.
    script = textwrap.dedent("""
        import sys, time
        for i in range(5):
            print("out-" + str(i), flush=True)
            time.sleep(0.05)
        sys.exit(1)
    """)
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(script),
        identity=_stub_identity(),
        log_dir=tmp_path,
        timeout_seconds=5,
    )
    threads_before = {t.ident for t in threading.enumerate()}

    with pytest.raises(RoleSubprocessError):
        dispatcher.dispatch(
            role="planner", project="p", issue_number=1, ticket_id=1,
        )

    # Give a brief moment in case OS scheduler hasn't reaped yet — but
    # any reader thread should already have been join()ed.
    time.sleep(0.05)
    new_threads = [
        t for t in threading.enumerate()
        if t.ident not in threads_before and t.is_alive()
    ]
    # Filter to threads obviously belonging to our reader pool. We name
    # them via Thread(name=...) in the dispatcher; if that name shows
    # up still alive, that's a leak.
    leaked_readers = [t for t in new_threads if "role-stream-" in t.name]
    assert not leaked_readers, (
        f"reader threads leaked after dispatch raised: {leaked_readers}"
    )


def test_stream_to_log_failure_does_not_deadlock_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """If the log_file.write/flush itself raises mid-stream (disk full,
    closed file, whatever), the reader thread must NOT die silently.
    Silent death leaves the subprocess blocked on a full pipe buffer.

    We wrap the dispatcher's _stream_to_log so the write call raises
    after the first line. The reader must drain the rest of the pipe
    so the subprocess doesn't deadlock, and the failure must be
    observable via the standard logger.
    """
    # Subprocess emits 5000 lines fast — enough to saturate the OS pipe
    # buffer if the reader stops consuming.
    script = textwrap.dedent(f"""
        import sys
        for i in range(5000):
            print("line-" + str(i), flush=True)
            print("err-" + str(i), file=sys.stderr, flush=True)
        print({_HAPPY_OUTCOME!r}, flush=True)
    """)

    # Wrap the open() returned file object so .write() raises on the
    # 2nd call. Use a counter shared across stdout + stderr readers so
    # at least one of them trips.
    real_open = sd.open if hasattr(sd, "open") else open
    write_calls = {"n": 0}

    class _FlakyFile:
        def __init__(self, f):
            self._f = f
            self.closed = False

        def write(self, s):
            write_calls["n"] += 1
            if write_calls["n"] == 2:
                raise OSError("simulated disk write failure")
            return self._f.write(s)

        def flush(self):
            return self._f.flush()

        def close(self):
            self.closed = True
            return self._f.close()

        def __getattr__(self, name):
            return getattr(self._f, name)

    def flaky_open(*args, **kwargs):
        return _FlakyFile(real_open(*args, **kwargs))

    monkeypatch.setattr(sd, "open", flaky_open, raising=False)

    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(script),
        identity=_stub_identity(),
        log_dir=tmp_path,
        timeout_seconds=15,
    )

    # Capture at the module logger directly. Earlier tests in the
    # suite may have called configure_logging() which sets propagate=False
    # on the foreman.v4 tree, so caplog's root-attached handler can't
    # see records originating below it. Attach a fresh handler instead.
    records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    list_handler = _ListHandler(level=logging.WARNING)
    module_logger = logging.getLogger("foreman.v4.subprocess_dispatcher")
    prior_level = module_logger.level
    module_logger.setLevel(logging.WARNING)
    module_logger.addHandler(list_handler)
    try:
        start = time.monotonic()
        # Whether dispatch returns normally or raises is implementation
        # choice; what matters is it RETURNS within reasonable time and
        # the failure is logged.
        try:
            dispatcher.dispatch(
                role="planner", project="p", issue_number=1, ticket_id=1,
            )
        except Exception:
            pass
        elapsed = time.monotonic() - start
    finally:
        module_logger.removeHandler(list_handler)
        module_logger.setLevel(prior_level)

    assert elapsed < 10.0, (
        f"dispatch hung {elapsed:.1f}s — subprocess likely deadlocked on "
        "full pipe buffer because the writer thread died silently"
    )
    # The writer failure must surface in logs (warning or exception level).
    failure_logged = any(
        "write" in rec.getMessage().lower()
        or "stream" in rec.getMessage().lower()
        or "drain" in rec.getMessage().lower()
        for rec in records
        if rec.levelno >= logging.WARNING
    )
    assert failure_logged, (
        f"writer failure was not logged; records: "
        f"{[(r.levelname, r.getMessage()) for r in records]}"
    )
    # Suppress unused-fixture warning — caplog reserved for future use.
    _ = caplog


def test_popen_uses_explicit_utf8_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """I1: Popen must be called with explicit ``encoding='utf-8'`` so
    the decode side doesn't depend on ``locale.getpreferredencoding()``
    — cp1252 on stock Windows, which mismatches the log file's utf-8
    encoding and corrupts non-ASCII output.

    We intercept the dispatcher's Popen call and assert the kwargs
    carry the explicit encoding. ``errors='replace'`` is belt-and-
    suspenders against truly bad bytes from a misbehaving child.
    """
    captured: dict = {}
    real_popen = subprocess.Popen

    def spy_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(sd.subprocess, "Popen", spy_popen)

    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(_outcome_script()),
        identity=_stub_identity(),
        log_dir=tmp_path,
    )
    dispatcher.dispatch(
        role="planner", project="p", issue_number=1, ticket_id=1,
    )
    assert captured["kwargs"].get("encoding") == "utf-8", (
        f"Popen must specify encoding='utf-8' explicitly; got "
        f"kwargs={captured['kwargs']!r}"
    )
    assert captured["kwargs"].get("errors") == "replace", (
        f"Popen must specify errors='replace' as belt-and-suspenders; got "
        f"kwargs={captured['kwargs']!r}"
    )


def test_popen_utf8_roundtrips_non_ascii(tmp_path: Path):
    """Integration sanity for I1: child writes UTF-8 bytes, dispatcher
    decodes them, log file contains the same characters."""
    script = textwrap.dedent(f"""
        import sys
        sys.stdout.buffer.write("héllo \\U0001fab6\\n".encode("utf-8"))
        sys.stdout.flush()
        print({_HAPPY_OUTCOME!r}, flush=True)
    """)
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(script),
        identity=_stub_identity(),
        log_dir=tmp_path,
    )
    stdout = dispatcher.dispatch(
        role="planner", project="p", issue_number=1, ticket_id=1,
    )
    assert "héllo" in stdout
    assert "\U0001fab6" in stdout
    log_text = next((tmp_path / "planner").glob("*.log")).read_text(
        encoding="utf-8",
    )
    assert "héllo" in log_text
    assert "\U0001fab6" in log_text


def test_error_message_includes_log_path(tmp_path: Path):
    """I4: when RoleSubprocessError fires, the operator needs to know
    WHERE the log file is, not just that one exists somewhere."""
    # Non-zero exit, no outcome marker → raises.
    script = textwrap.dedent("""
        import sys
        print("boom", flush=True)
        sys.exit(7)
    """)
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=_python_script_cli(script),
        identity=_stub_identity(),
        log_dir=tmp_path,
    )
    with pytest.raises(RoleSubprocessError) as exc_info:
        dispatcher.dispatch(
            role="planner", project="p", issue_number=1, ticket_id=1,
        )
    msg = str(exc_info.value)
    log_path = next((tmp_path / "planner").glob("*.log"))
    assert str(log_path) in msg, (
        f"error message {msg!r} does not contain log path {log_path!s}"
    )


def test_timeout_error_message_includes_log_path(tmp_path: Path):
    """I4: same shape as exit-without-outcome, but for the TIMEOUT branch."""
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
    with pytest.raises(RoleSubprocessError) as exc_info:
        dispatcher.dispatch(
            role="planner", project="p", issue_number=1, ticket_id=1,
        )
    msg = str(exc_info.value)
    log_path = next((tmp_path / "planner").glob("*.log"))
    assert str(log_path) in msg, (
        f"timeout error message {msg!r} does not contain log path {log_path!s}"
    )
