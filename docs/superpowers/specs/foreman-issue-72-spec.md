# Spec: make `foreman daemon start` write a pid file so `daemon stop` can see it (issue #72)

## Goal

Fix the `foreman daemon` CLI so `daemon stop` and `daemon status` can
actually see a running daemon. Today neither command works: `daemon
start` (`packages/foreman/src/foreman/cli.py:318-321`) launches the
async daemon but never writes `~/.foreman/daemon.pid`, while
`daemon stop` (`cli.py:324-337`) and `daemon status` (`cli.py:340-352`)
both look for that file. Result: a daemon that has been running
overnight reports as "not running" and cannot be stopped without
finding its PID via `Get-CimInstance Win32_Process` (caught
2026-06-02 on Jeff's Windows host).

Tracks issue [#72](https://github.com/jeffrichley/foreman/issues/72).

## Acceptance criteria

- `packages/foreman/src/foreman/config.py`'s `DaemonConfig` gains a
  new `pid_path: str = Field(default="~/.foreman/daemon.pid")` field,
  matching the shape of the existing `log_path` and `sqlite_path`
  defaults so the path is config-overridable for tests and
  multi-daemon setups.
- A new module-private helper
  `_resolve_pid_path(config: Config | None) -> Path` in
  `packages/foreman/src/foreman/cli.py` returns
  `Path(config.daemon.pid_path).expanduser()` when `config` is
  provided, and `Path("~/.foreman/daemon.pid").expanduser()` when
  `config` is `None` (the fallback path so `daemon stop` / `daemon
  status` still work when the user has no `~/.foreman/config.toml`).
- New module-private helpers in `cli.py`:
  - `_check_or_remove_stale_pid_file(pid_path: Path) -> None` — if
    the file is absent, returns. If present but unreadable / contains
    a non-int, deletes it (treat as stale). If present and the PID
    is dead (per `os.kill(pid, 0)`), deletes it. If present and the
    PID is alive, raises `click.ClickException` so `daemon start`
    aborts with a clear "already running (pid <N>)" message.
  - `_write_pid_file(pid_path: Path) -> None` — `mkdir -p` the
    parent, then write `str(os.getpid())` atomically (write to a
    `.tmp` sibling, then `replace`).
  - `_remove_pid_file(pid_path: Path) -> None` — `pid_path.unlink(
    missing_ok=True)`.
- `daemon_start` (`cli.py:318-321`) loads config, resolves
  `pid_path` via `_resolve_pid_path`, calls
  `_check_or_remove_stale_pid_file`, calls `_write_pid_file`, then
  wraps `asyncio.run(_daemon_run(...))` in a `try/finally` whose
  finally calls `_remove_pid_file`. The pid file's PID must equal
  `os.getpid()` of the `foreman daemon start` process — verified by
  a CliRunner test that captures the pid file's contents while the
  daemon is running.
- `daemon_stop` (`cli.py:324-337`):
  - Resolves `pid_path` via `_resolve_pid_path`, falling back to the
    no-config default if config loading raises (so the operator can
    stop the daemon from a host where the config file has been
    removed).
  - If the pid file is missing, emits the new message: `"No daemon
    pid file at <path>. Either the daemon was never started, or it
    exited uncleanly. To find a stray process: `tasklist | findstr
    foreman` (Windows) or `ps aux | grep foreman` (POSIX), then
    kill the PID directly."`. Distinguishes "never started" from
    "missing but maybe running" by suggesting the discovery command.
  - If the pid file is present, reads the PID, calls
    `os.kill(pid, signal.SIGTERM)`. On `ProcessLookupError` (the PID
    is already dead), removes the stale file and reports it.
  - After SIGTERM, polls the pid file's existence for up to 10s
    (sleeping 100ms between checks); if the file disappears, prints
    `"Daemon stopped cleanly."`. If the file is still present after
    10s, prints a warning and unlinks the file ourselves (handles
    Windows, where `os.kill(pid, SIGTERM)` calls `TerminateProcess`
    and the daemon never reaches its cleanup finally).
- `daemon_status` (`cli.py:340-352`) resolves `pid_path` via the
  same helper (with config fallback) so the displayed status path
  matches the path `daemon start` actually wrote. Behavior is
  otherwise unchanged.
- The existing test
  `test_daemon_start_foreground_runs_and_exits_clean` (`test_cli.py:440-455`)
  is updated to set `pid_path` in the test config to a
  `tmp_path` location, so the test does NOT overwrite a real
  user's `~/.foreman/daemon.pid` if a real daemon happens to be
  running on the host.
- New tests in `packages/foreman/tests/test_cli.py`:
  - `test_write_pid_file_creates_parent_and_writes_current_pid` —
    pass a `tmp_path / "nested" / "daemon.pid"` (parent does not
    exist), call `_write_pid_file`, assert the file exists, parent
    was created, and contents `int()` to `os.getpid()`.
  - `test_check_or_remove_stale_pid_file_no_file_is_noop` — pass a
    path that doesn't exist, assert no exception and no file created.
  - `test_check_or_remove_stale_pid_file_corrupt_file_removed` —
    write `"not a number"` to the path, call the helper, assert
    file is gone and no exception.
  - `test_check_or_remove_stale_pid_file_dead_pid_removed` — write a
    PID that's definitely dead (e.g., `2**31 - 1`) to the path, call
    the helper, assert file is gone and no exception. Skip on
    Windows where `os.kill(LARGE_PID, 0)` may raise the wrong
    exception class; on Windows, monkeypatch `os.kill` to raise
    `ProcessLookupError`.
  - `test_check_or_remove_stale_pid_file_live_pid_raises` — write
    `str(os.getpid())` (the test process is alive) to the path, call
    the helper, assert `click.ClickException` is raised and the
    file is NOT removed.
  - `test_daemon_start_writes_pid_file_and_cleans_up` — write a
    config with `pid_path = "<tmp_path>/d.pid"`, monkeypatch
    `foreman.cli._daemon_run` with an async spy that captures
    `pid_path.exists()` and `int(pid_path.read_text())` mid-flight,
    invoke `daemon start --max-iterations 1` via `CliRunner`, assert
    the spy captured `exists=True` and `pid=os.getpid()`, and assert
    `pid_path.exists() is False` after the command returns.
  - `test_daemon_start_aborts_when_live_pid_file_present` — write a
    pid file containing `os.getpid()` (test process is alive), invoke
    `daemon start --max-iterations 1`, assert exit code is non-zero
    and the output contains `"already running"`. Assert the pid file
    is still present (we don't remove a live daemon's file).
  - `test_daemon_start_removes_stale_pid_file_and_starts` — write a
    pid file with a dead PID, monkeypatch `_daemon_run` with a
    no-op coroutine, invoke `daemon start --max-iterations 1`, assert
    exit code 0, and assert the pid file at the end is gone (the
    finally cleanup ran).
  - `test_daemon_stop_reads_pid_file_and_sends_sigterm` — write a
    pid file with `os.getpid()`, monkeypatch `os.kill` so we capture
    the args without actually signalling the test process, also
    monkeypatch the polling sleep to a no-op and arrange for the
    file to be removed after the (mocked) SIGTERM call so the poll
    sees clean exit, invoke `daemon stop`, assert
    `os.kill` was called with `(os.getpid(), signal.SIGTERM)`, and
    assert the output contains `"Daemon stopped cleanly."`.
  - `test_daemon_stop_falls_back_unlink_on_timeout` — write a pid
    file with `os.getpid()`, monkeypatch `os.kill` to no-op,
    monkeypatch the poll-timeout to a tiny value (e.g., total wait
    100ms) so the loop expires fast, invoke `daemon stop`, assert
    the pid file was removed by `stop` itself and the output
    contains a warning about the daemon not exiting in time.
  - `test_daemon_stop_with_missing_pid_file_gives_actionable_message` —
    no pid file present, invoke `daemon stop`, assert exit code 0
    and assert the output contains `"tasklist"`, `"foreman"`, AND
    the resolved pid path. Validates the new diagnostic message.
  - `test_daemon_stop_with_dead_pid_removes_stale_file` — write a
    pid file with a dead PID, monkeypatch `os.kill` to raise
    `ProcessLookupError`, invoke `daemon stop`, assert exit code 0,
    the output mentions the stale file removal, and the pid file is
    gone.
  - `test_daemon_stop_works_without_config_file` — do NOT create a
    config file, do NOT set `FOREMAN_CONFIG`, point HOME to
    `tmp_path` via `monkeypatch.setenv("HOME", str(tmp_path))` AND
    `monkeypatch.setenv("USERPROFILE", str(tmp_path))` (Windows uses
    USERPROFILE), write a pid file at `tmp_path / ".foreman" /
    "daemon.pid"` containing `os.getpid()`, monkeypatch `os.kill`,
    invoke `daemon stop`, assert exit code 0 and SIGTERM was sent.
    Proves the config-missing fallback works.
- A subprocess-based end-to-end test
  `test_daemon_start_stop_subprocess` in `tests/test_cli.py`, guarded
  by `@pytest.mark.skipif(sys.platform == "win32", reason="...")`:
  spawns the daemon as `subprocess.Popen([sys.executable, "-m",
  "foreman.cli", "daemon", "start"], env=...)` with a tmp-path
  config that points `pid_path` / `sqlite_path` / `log_path` /
  `FOREMAN_CONFIG` into `tmp_path`, polls until the pid file
  appears (up to 30s), runs `subprocess.run([sys.executable, "-m",
  "foreman.cli", "daemon", "stop"], ...)`, asserts stop returncode
  is 0, waits for the original Popen to exit with `proc.wait(
  timeout=15)`, and asserts the pid file is gone. This is the
  literal acceptance criterion from the issue body.
- `packages/foreman/src/foreman/cli.py` must remain runnable as a
  module via `python -m foreman.cli` so the subprocess test can
  spawn it without depending on `uv` being on PATH. The file
  already has `def main(): cli()` plus a console-script entry; add
  an `if __name__ == "__main__": main()` block at the end if absent
  (verify by reading the file).
- `just check` exits zero. The change touches only `cli.py`,
  `config.py`, and `tests/test_cli.py`; existing daemon-class and
  poller-class tests must continue to pass.

## Approach

The bug has one cause: nothing writes `~/.foreman/daemon.pid` on the
start path. `daemon_start` calls `asyncio.run(_daemon_run(...))`
straight into the async loop and never touches the pid file. The
stop and status commands then correctly report "no pid file found"
because none exists — the diagnostic is honest, but the underlying
contract is broken on the producer side.

The fix is structurally simple and stays inside `cli.py` (plus one
new field on `DaemonConfig`). We don't push pid-file logic into the
`Daemon` class itself: a process pid file is a CLI / OS concern,
not a property of the daemon's async runtime, and the existing
`Daemon` is already tested with in-process e2e tests
(`test_daemon_e2e.py`) that have no notion of a pid file. Keeping
the pid file in the CLI layer also means `--max-iterations` test
runs still write+clean the pid file the same way production does,
which simplifies the test surface.

Three helpers carry the logic:

1. `_check_or_remove_stale_pid_file(pid_path)` — the v1 of issue
   acceptance criterion #3 ("atomically delete a stale pid file on
   startup"). Strategy: if file present, parse PID; if parse fails
   OR `os.kill(pid, 0)` raises `ProcessLookupError` / `OSError`,
   delete and proceed; if the PID is alive, raise
   `click.ClickException("Foreman daemon already running (pid <N>).
   Stop it first via `foreman daemon stop`.")` so the start aborts
   loudly. This matches `daemon_status`'s existing `os.kill(pid, 0)`
   liveness probe (`cli.py:349`) so we're not introducing a new
   liveness primitive.

2. `_write_pid_file(pid_path)` — `pid_path.parent.mkdir(
   parents=True, exist_ok=True)` then atomic write via
   `Path.write_text` on a `.tmp` sibling followed by
   `os.replace(tmp, pid_path)`. Atomic write avoids the rare race
   where a concurrent `daemon stop` sees a half-written file.

3. `_remove_pid_file(pid_path)` — `pid_path.unlink(missing_ok=True)`.

`daemon_start`'s new shape:

```python
@daemon.command("start")
@click.option("--max-iterations", type=int, default=None, ...)
def daemon_start(max_iterations: int | None) -> None:
    """Start the daemon in foreground."""
    config = _load_config_from_env()
    pid_path = _resolve_pid_path(config)
    _check_or_remove_stale_pid_file(pid_path)
    _write_pid_file(pid_path)
    try:
        asyncio.run(_daemon_run(config=config, max_iterations=max_iterations))
    finally:
        _remove_pid_file(pid_path)
```

The `try / finally` covers every exit path the foreground daemon
has today: SIGTERM via signal handler, `--max-iterations` natural
exit, KeyboardInterrupt, and uncaught exception. (`asyncio.run`
returns normally in the first two and re-raises in the third, both
of which run the finally.)

`daemon_stop`'s new shape needs to handle three cases:

- **Pid file present, PID alive** (the happy path): SIGTERM, wait
  up to 10s for the daemon's finally cleanup to remove the pid
  file, fall back to unlinking ourselves if it doesn't. The fall-
  back exists for Windows, where `os.kill(pid, signal.SIGTERM)`
  invokes `TerminateProcess` (hard kill) and the daemon never
  reaches its finally. Cross-platform `stop` thus always ends with
  the pid file gone.
- **Pid file present, PID dead** (post-crash): unlink with a
  message. Same as today.
- **Pid file absent** (the bug scenario the issue raised): emit a
  diagnostic message that names the resolved pid path AND suggests
  the platform-appropriate process-discovery command (`tasklist |
  findstr foreman` on Windows, `ps aux | grep foreman` on POSIX),
  so the operator can find a stray daemon process without us
  taking on the complexity of scanning ourselves.

Why not the auto-scan-and-kill fallback from issue option #2? Two
reasons: (a) cross-platform process-table scanning is its own
project — `psutil` would be the right tool but we'd add a
dependency for one rarely-hit fallback path, (b) the auto-scan
would kill ANY `foreman.*daemon` process, including ones a
developer is running in another terminal for debugging, which is
worse DX than the clearer message. We pick the clearer message;
the operator decides whether to kill.

The new pid-path resolution helper exists because we want
`daemon stop` / `daemon status` to work even when `~/.foreman/config.toml`
is absent (no config → no daemon → "not running" should still be
the correct answer, not a `FileNotFoundError` traceback). The
helper wraps `_load_config_from_env` in a try/except: on success,
use the config's `daemon.pid_path`; on failure, use the literal
default `~/.foreman/daemon.pid`. This matches how `daemon stop`
behaves today (no config load at all) without breaking the
config-driven path for `daemon start`.

Tests live in `test_cli.py` to keep the daemon-CLI contract pinned
in one file. The unit tests cover each helper in isolation. The
integration test for `daemon start` uses a monkeypatched
`_daemon_run` spy so we can inspect the pid file mid-run without
needing a subprocess. The subprocess test covers the literal
issue-body acceptance ("starts the daemon in a subprocess, asserts
the pid file appears, asserts `stop` cleanly terminates") for the
POSIX path; Windows is skipped because `subprocess.Popen` started
without `creationflags=CREATE_NEW_PROCESS_GROUP` plus
`TerminateProcess` semantics make graceful-stop subprocess testing
brittle on Windows — that's a separate concern (see Out of scope).

The existing test
`test_daemon_start_foreground_runs_and_exits_clean` must be
updated to specify `pid_path` in its tmp-path config: without
that, the test would write a pid file to the real user's
`~/.foreman/daemon.pid` during `pytest`, which is both wrong
(tests should not touch real user state) and dangerous (could
collide with a real running daemon's pid file). This is a
one-line config-text change in the test.

## Sub-requests (topologically sorted)

1. Add `pid_path` to `DaemonConfig` in
   `packages/foreman/src/foreman/config.py`. Place it adjacent to
   `log_path` and `sqlite_path` (current lines 75-77) so the
   defaults read as a coherent group:

   ```python
   log_path: str = Field(default="~/.foreman/daemon.log")
   log_level: str = Field(default="INFO")
   sqlite_path: str = Field(default="~/.foreman/foreman.sqlite")
   pid_path: str = Field(default="~/.foreman/daemon.pid")
   ```

   No validators needed — it's a simple path string.

2. Add the three pid-file helpers and the pid-path resolver near
   the existing daemon helpers in
   `packages/foreman/src/foreman/cli.py`. Place them above the
   `daemon` click group (currently line 306) so they're in scope
   for all three subcommands. Implementation:

   ```python
   def _resolve_pid_path(config: Config | None) -> Path:
       """Return the daemon's pid-file path.

       When ``config`` is provided, returns its configured
       ``daemon.pid_path``. When ``None`` (e.g., ``daemon stop``
       called on a host without a config file), falls back to the
       hardcoded default so the stop / status commands still work
       without config.
       """
       if config is None:
           return Path("~/.foreman/daemon.pid").expanduser()
       return Path(config.daemon.pid_path).expanduser()


   def _check_or_remove_stale_pid_file(pid_path: Path) -> None:
       """Raise ClickException if a live daemon's pid file exists.

       Idempotent stale-file cleanup. If the pid file is absent or
       corrupted or references a dead PID, deletes any stale file
       and returns. If the pid file references a live PID, raises
       ``click.ClickException`` so ``daemon start`` aborts loudly.
       (foreman#72: this is "the daemon detects stale pid files on
       startup" half of the contract; the other half is the
       try/finally cleanup on graceful exit.)
       """
       if not pid_path.exists():
           return
       try:
           existing_pid = int(pid_path.read_text().strip())
       except (ValueError, OSError):
           pid_path.unlink(missing_ok=True)
           return
       try:
           os.kill(existing_pid, 0)
       except (ProcessLookupError, OSError):
           # Dead or unreachable; treat as stale.
           pid_path.unlink(missing_ok=True)
           return
       raise click.ClickException(
           f"Foreman daemon already running (pid {existing_pid}). "
           f"Stop it first via `foreman daemon stop`."
       )


   def _write_pid_file(pid_path: Path) -> None:
       """Write the current process's PID to ``pid_path`` atomically."""
       pid_path.parent.mkdir(parents=True, exist_ok=True)
       tmp = pid_path.with_suffix(pid_path.suffix + ".tmp")
       tmp.write_text(str(os.getpid()))
       os.replace(tmp, pid_path)


   def _remove_pid_file(pid_path: Path) -> None:
       """Idempotent pid-file cleanup."""
       pid_path.unlink(missing_ok=True)
   ```

3. Update `daemon_start` in
   `packages/foreman/src/foreman/cli.py` (currently lines 311-321)
   to write+cleanup the pid file around the asyncio run:

   ```python
   @daemon.command("start")
   @click.option(
       "--max-iterations",
       type=int,
       default=None,
       help="Stop after N worker iterations (testing only).",
   )
   def daemon_start(max_iterations: int | None) -> None:
       """Start the daemon in foreground."""
       config = _load_config_from_env()
       pid_path = _resolve_pid_path(config)
       _check_or_remove_stale_pid_file(pid_path)
       _write_pid_file(pid_path)
       try:
           asyncio.run(_daemon_run(config=config, max_iterations=max_iterations))
       finally:
           _remove_pid_file(pid_path)
   ```

4. Update `daemon_stop` in `cli.py` (currently lines 324-337)
   to use the resolved pid path and wait for graceful shutdown:

   ```python
   _STOP_GRACE_SECONDS = 10.0
   _STOP_POLL_INTERVAL_SECONDS = 0.1


   @daemon.command("stop")
   def daemon_stop() -> None:
       """Signal a running daemon to stop and wait for clean exit."""
       try:
           config: Config | None = _load_config_from_env()
       except (FileNotFoundError, OSError):
           config = None
       pid_path = _resolve_pid_path(config)

       if not pid_path.exists():
           import sys as _sys
           discover = (
               "tasklist | findstr foreman"
               if _sys.platform == "win32"
               else "ps aux | grep foreman"
           )
           click.echo(
               f"No daemon pid file at {pid_path}. Either the daemon "
               f"was never started, or it exited uncleanly. To find a "
               f"stray process: `{discover}`, then kill the PID directly."
           )
           return

       pid = int(pid_path.read_text().strip())
       try:
           os.kill(pid, signal.SIGTERM)
       except ProcessLookupError:
           click.echo(f"Pid {pid} not running. Removing stale pid file.")
           pid_path.unlink(missing_ok=True)
           return
       click.echo(f"Sent SIGTERM to daemon pid {pid}; waiting for graceful shutdown.")

       # Poll for the daemon's finally-clause cleanup. On Windows,
       # os.kill(pid, SIGTERM) calls TerminateProcess (hard kill) and
       # the daemon never reaches its finally — so we unlink ourselves
       # after the grace period.
       import time as _time
       deadline = _time.monotonic() + _STOP_GRACE_SECONDS
       while _time.monotonic() < deadline:
           if not pid_path.exists():
               click.echo("Daemon stopped cleanly.")
               return
           _time.sleep(_STOP_POLL_INTERVAL_SECONDS)
       click.echo(
           f"Daemon did not remove its pid file within "
           f"{_STOP_GRACE_SECONDS}s; cleaning up {pid_path} ourselves."
       )
       pid_path.unlink(missing_ok=True)
   ```

5. Update `daemon_status` in `cli.py` (currently lines 340-352)
   to use the resolved pid path with config fallback:

   ```python
   @daemon.command("status")
   def daemon_status() -> None:
       """Show daemon status — running / stopped."""
       try:
           config: Config | None = _load_config_from_env()
       except (FileNotFoundError, OSError):
           config = None
       pid_path = _resolve_pid_path(config)
       if not pid_path.exists():
           click.echo("Daemon: not running.")
           return
       pid = int(pid_path.read_text().strip())
       try:
           os.kill(pid, 0)
           click.echo(f"Daemon: running (pid {pid}).")
       except (ProcessLookupError, OSError):
           click.echo(
               f"Daemon: stale pid file (pid {pid} dead). "
               f"Run `foreman daemon stop` to clean."
           )
   ```

6. Verify the bottom of `cli.py` has the module-runnable guard.
   Check whether the file already ends with:

   ```python
   def main() -> None:
       """Console-script entry point."""
       cli()
   ```

   If there's no `if __name__ == "__main__": main()` line below it,
   add one — the subprocess integration test in step 12 invokes
   `python -m foreman.cli daemon start`, which needs the module
   guard to fire `main()`. (Existing `def main(): cli()` is
   compatible; just append the guard if missing.)

7. Update the existing test
   `test_daemon_start_foreground_runs_and_exits_clean` in
   `packages/foreman/tests/test_cli.py` (currently lines 440-455)
   to add `pid_path = "<tmp_path>/d.pid"` to the test config so
   the test does not write to the real user's
   `~/.foreman/daemon.pid`. Replace the `config_path.write_text`
   block's `[daemon]` section with:

   ```python
   config_path.write_text(
       f'[admin]\ngithub_token_env = "X"\n'
       f'[daemon]\nsqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
       f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
       f'pid_path = "{(tmp_path / "d.pid").as_posix()}"\n'
   )
   ```

   At the end of the test, also assert the pid file no longer
   exists (this is the cleanup contract):

   ```python
   assert result.exit_code == 0
   assert not (tmp_path / "d.pid").exists()
   ```

8. Add the helper unit tests to `tests/test_cli.py`. Place them
   in a new section under a comment header `# --- pid-file
   helpers (foreman#72) ---` near the other daemon tests. Tests:

   ```python
   def test_write_pid_file_creates_parent_and_writes_current_pid(
       tmp_path: Path,
   ) -> None:
       from foreman.cli import _write_pid_file

       pid_path = tmp_path / "nested" / "deeper" / "daemon.pid"
       _write_pid_file(pid_path)

       assert pid_path.exists()
       assert pid_path.parent.exists()
       assert int(pid_path.read_text().strip()) == os.getpid()


   def test_check_or_remove_stale_pid_file_no_file_is_noop(
       tmp_path: Path,
   ) -> None:
       from foreman.cli import _check_or_remove_stale_pid_file

       pid_path = tmp_path / "daemon.pid"
       _check_or_remove_stale_pid_file(pid_path)  # no exception

       assert not pid_path.exists()


   def test_check_or_remove_stale_pid_file_corrupt_file_removed(
       tmp_path: Path,
   ) -> None:
       from foreman.cli import _check_or_remove_stale_pid_file

       pid_path = tmp_path / "daemon.pid"
       pid_path.write_text("not a number")
       _check_or_remove_stale_pid_file(pid_path)

       assert not pid_path.exists()


   def test_check_or_remove_stale_pid_file_dead_pid_removed(
       tmp_path: Path, monkeypatch
   ) -> None:
       from foreman.cli import _check_or_remove_stale_pid_file

       pid_path = tmp_path / "daemon.pid"
       pid_path.write_text("999999999")

       def _fake_kill(pid: int, sig: int) -> None:
           raise ProcessLookupError

       monkeypatch.setattr("foreman.cli.os.kill", _fake_kill)
       _check_or_remove_stale_pid_file(pid_path)

       assert not pid_path.exists()


   def test_check_or_remove_stale_pid_file_live_pid_raises(
       tmp_path: Path,
   ) -> None:
       import click as _click

       from foreman.cli import _check_or_remove_stale_pid_file

       pid_path = tmp_path / "daemon.pid"
       pid_path.write_text(str(os.getpid()))

       import pytest as _pytest

       with _pytest.raises(_click.ClickException) as excinfo:
           _check_or_remove_stale_pid_file(pid_path)

       assert "already running" in str(excinfo.value)
       assert pid_path.exists()  # live file is NOT removed
   ```

   Add the imports at the top of the file (`import os`, `import
   pytest`) if not already present.

9. Add the in-process integration tests for `daemon start` to
   `tests/test_cli.py`:

   ```python
   def test_daemon_start_writes_pid_file_and_cleans_up(
       tmp_path: Path, monkeypatch
   ) -> None:
       """daemon_start must write the pid file before the daemon
       runs and remove it on exit (foreman#72 acceptance #1)."""
       pid_path = tmp_path / "d.pid"
       config_path = tmp_path / "config.toml"
       config_path.write_text(
           f'[admin]\ngithub_token_env = "X"\n'
           f'[daemon]\n'
           f'pid_path = "{pid_path.as_posix()}"\n'
           f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
           f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
       )
       monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

       captured: dict[str, object] = {}

       async def spy(*, config, max_iterations):  # noqa: ARG001
           captured["exists_mid"] = pid_path.exists()
           captured["pid_mid"] = (
               int(pid_path.read_text().strip())
               if pid_path.exists()
               else None
           )

       monkeypatch.setattr("foreman.cli._daemon_run", spy)

       result = CliRunner().invoke(
           cli, ["daemon", "start", "--max-iterations", "1"]
       )
       assert result.exit_code == 0, result.output
       assert captured["exists_mid"] is True
       assert captured["pid_mid"] == os.getpid()
       assert not pid_path.exists()


   def test_daemon_start_aborts_when_live_pid_file_present(
       tmp_path: Path, monkeypatch
   ) -> None:
       """daemon_start must refuse to start if a live pid file
       exists (foreman#72 acceptance #3)."""
       pid_path = tmp_path / "d.pid"
       pid_path.write_text(str(os.getpid()))  # test process is alive

       config_path = tmp_path / "config.toml"
       config_path.write_text(
           f'[admin]\ngithub_token_env = "X"\n'
           f'[daemon]\n'
           f'pid_path = "{pid_path.as_posix()}"\n'
           f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
           f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
       )
       monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

       result = CliRunner().invoke(
           cli, ["daemon", "start", "--max-iterations", "1"]
       )
       assert result.exit_code != 0
       assert "already running" in result.output
       assert pid_path.exists()  # live file not removed


   def test_daemon_start_removes_stale_pid_file_and_starts(
       tmp_path: Path, monkeypatch
   ) -> None:
       """daemon_start must clear a stale pid file (dead PID) and
       proceed (foreman#72 acceptance #3)."""
       pid_path = tmp_path / "d.pid"
       pid_path.write_text("999999999")  # definitely dead

       config_path = tmp_path / "config.toml"
       config_path.write_text(
           f'[admin]\ngithub_token_env = "X"\n'
           f'[daemon]\n'
           f'pid_path = "{pid_path.as_posix()}"\n'
           f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
           f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
       )
       monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

       # Pretend the dead PID is dead on every platform.
       def _fake_kill(pid: int, sig: int) -> None:
           if sig == 0:
               raise ProcessLookupError

       monkeypatch.setattr("foreman.cli.os.kill", _fake_kill)

       async def noop(*, config, max_iterations):  # noqa: ARG001
           pass

       monkeypatch.setattr("foreman.cli._daemon_run", noop)

       result = CliRunner().invoke(
           cli, ["daemon", "start", "--max-iterations", "1"]
       )
       assert result.exit_code == 0, result.output
       assert not pid_path.exists()
   ```

10. Add the `daemon stop` unit tests to `tests/test_cli.py`:

    ```python
    def test_daemon_stop_reads_pid_file_and_sends_sigterm(
        tmp_path: Path, monkeypatch
    ) -> None:
        """daemon_stop must read the pid file and send SIGTERM
        (foreman#72 acceptance #2)."""
        pid_path = tmp_path / "d.pid"
        pid_path.write_text(str(os.getpid()))

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'[admin]\ngithub_token_env = "X"\n'
            f'[daemon]\npid_path = "{pid_path.as_posix()}"\n'
        )
        monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

        kill_calls: list[tuple[int, int]] = []

        def _fake_kill(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))
            # Simulate a graceful daemon: remove the pid file
            # immediately so the stop-poll sees clean exit.
            pid_path.unlink(missing_ok=True)

        monkeypatch.setattr("foreman.cli.os.kill", _fake_kill)

        result = CliRunner().invoke(cli, ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert kill_calls == [(os.getpid(), signal.SIGTERM)]
        assert "Daemon stopped cleanly." in result.output


    def test_daemon_stop_falls_back_unlink_on_timeout(
        tmp_path: Path, monkeypatch
    ) -> None:
        """When the daemon doesn't remove its pid file in time
        (e.g., Windows TerminateProcess hard kill), `stop`
        unlinks the file itself (foreman#72 cross-platform
        guarantee)."""
        pid_path = tmp_path / "d.pid"
        pid_path.write_text(str(os.getpid()))

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'[admin]\ngithub_token_env = "X"\n'
            f'[daemon]\npid_path = "{pid_path.as_posix()}"\n'
        )
        monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

        monkeypatch.setattr(
            "foreman.cli.os.kill", lambda pid, sig: None
        )
        monkeypatch.setattr(
            "foreman.cli._STOP_GRACE_SECONDS", 0.05
        )
        monkeypatch.setattr(
            "foreman.cli._STOP_POLL_INTERVAL_SECONDS", 0.01
        )

        result = CliRunner().invoke(cli, ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert "did not remove its pid file" in result.output
        assert not pid_path.exists()


    def test_daemon_stop_with_missing_pid_file_gives_actionable_message(
        tmp_path: Path, monkeypatch
    ) -> None:
        """daemon_stop's missing-pid-file message must name a
        discovery command so the operator can find a stray
        daemon process (foreman#72 acceptance #3)."""
        pid_path = tmp_path / "d.pid"  # never created
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'[admin]\ngithub_token_env = "X"\n'
            f'[daemon]\npid_path = "{pid_path.as_posix()}"\n'
        )
        monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

        result = CliRunner().invoke(cli, ["daemon", "stop"])

        assert result.exit_code == 0
        assert str(pid_path) in result.output
        assert "foreman" in result.output
        # One of the platform discovery commands must appear:
        assert ("tasklist" in result.output) or (
            "ps aux" in result.output
        )


    def test_daemon_stop_with_dead_pid_removes_stale_file(
        tmp_path: Path, monkeypatch
    ) -> None:
        pid_path = tmp_path / "d.pid"
        pid_path.write_text("999999999")
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'[admin]\ngithub_token_env = "X"\n'
            f'[daemon]\npid_path = "{pid_path.as_posix()}"\n'
        )
        monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

        def _fake_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError

        monkeypatch.setattr("foreman.cli.os.kill", _fake_kill)

        result = CliRunner().invoke(cli, ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert "not running" in result.output
        assert not pid_path.exists()


    def test_daemon_stop_works_without_config_file(
        tmp_path: Path, monkeypatch
    ) -> None:
        """Operator without a config file can still stop the
        daemon — falls back to ~/.foreman/daemon.pid via
        $HOME / $USERPROFILE (foreman#72)."""
        monkeypatch.delenv("FOREMAN_CONFIG", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        default_pid_path = tmp_path / ".foreman" / "daemon.pid"
        default_pid_path.parent.mkdir(parents=True, exist_ok=True)
        default_pid_path.write_text(str(os.getpid()))

        def _fake_kill(pid: int, sig: int) -> None:
            default_pid_path.unlink(missing_ok=True)

        monkeypatch.setattr("foreman.cli.os.kill", _fake_kill)

        result = CliRunner().invoke(cli, ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert "Daemon stopped cleanly." in result.output
    ```

11. Update `test_daemon_status_when_not_running` (already at
    `test_cli.py:426-437`) to set `pid_path` in the config so the
    test does not look at the real user's
    `~/.foreman/daemon.pid`. Replace the config write with:

    ```python
    config_path.write_text(
        f'[admin]\ngithub_token_env = "X"\n'
        f'[daemon]\npid_path = "{(tmp_path / "d.pid").as_posix()}"\n'
    )
    ```

    Existing assertion (`"not running" in result.output.lower()`)
    continues to hold because the tmp pid file does not exist.

12. Add the subprocess integration test, guarded by skipif Windows:

    ```python
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "SIGTERM via os.kill on Windows is TerminateProcess "
            "(hard kill); subprocess graceful-stop semantics are "
            "tested separately at the unit level."
        ),
    )
    def test_daemon_start_stop_subprocess(tmp_path: Path) -> None:
        """End-to-end: spawn `foreman daemon start` as a real
        subprocess, wait for the pid file, run `foreman daemon
        stop`, assert clean exit and pid-file cleanup
        (foreman#72 issue-body acceptance criterion)."""
        import subprocess
        import sys
        import time

        pid_path = tmp_path / "d.pid"
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'[admin]\ngithub_token_env = "X"\n'
            f'[daemon]\n'
            f'pid_path = "{pid_path.as_posix()}"\n'
            f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
            f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
        )
        env = {**os.environ, "FOREMAN_CONFIG": str(config_path)}

        proc = subprocess.Popen(
            [sys.executable, "-m", "foreman.cli", "daemon", "start"],
            env=env,
        )
        try:
            # Poll for pid file (up to 30s).
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if pid_path.exists():
                    break
                time.sleep(0.1)
            assert pid_path.exists(), (
                "daemon never wrote pid file within 30s"
            )
            written_pid = int(pid_path.read_text().strip())
            assert written_pid == proc.pid

            stop = subprocess.run(
                [
                    sys.executable, "-m", "foreman.cli",
                    "daemon", "stop",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert stop.returncode == 0, stop.stderr or stop.stdout
            assert "Daemon stopped cleanly." in stop.stdout

            proc.wait(timeout=15)
            assert proc.returncode == 0
            assert not pid_path.exists()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    ```

    Add `import sys` and `import pytest` at the top of the test
    file if not already present.

13. Run targeted tests first to verify the helpers + integration
    tests pass:

    ```bash
    uv run pytest packages/foreman/tests/test_cli.py -k "daemon or pid_file" -v
    ```

14. Run `just check` and confirm exit zero.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/config.py` | Add `pid_path: str = Field(default="~/.foreman/daemon.pid")` to `DaemonConfig`, adjacent to the existing `log_path` / `sqlite_path` defaults. No validator. |
| `packages/foreman/src/foreman/cli.py` | Add four module-private helpers (`_resolve_pid_path`, `_check_or_remove_stale_pid_file`, `_write_pid_file`, `_remove_pid_file`) plus two module-level grace constants (`_STOP_GRACE_SECONDS = 10.0`, `_STOP_POLL_INTERVAL_SECONDS = 0.1`). Rewrite `daemon_start` to write the pid file via the helpers and wrap the asyncio.run in try/finally cleanup. Rewrite `daemon_stop` to resolve the pid path through config (with no-config fallback), poll for graceful shutdown, and unlink as cross-platform backstop. Rewrite `daemon_status` to use the resolved pid path with the same config fallback. Verify (and add if missing) `if __name__ == "__main__": main()` so the subprocess test can spawn `python -m foreman.cli`. |
| `packages/foreman/tests/test_cli.py` | Update the existing `test_daemon_status_when_not_running` and `test_daemon_start_foreground_runs_and_exits_clean` to set `pid_path` in the test config (avoid touching the real user's `~/.foreman/daemon.pid`). Add helper unit tests, three `daemon_start` integration tests (write+cleanup, abort on live pid, clear stale pid), five `daemon_stop` unit tests (sigterm + clean exit, timeout fallback unlink, missing-file diagnostic, dead-pid stale cleanup, no-config fallback), and one subprocess end-to-end test guarded by `skipif sys.platform == "win32"`. Add `import os`, `import signal`, `import sys`, `import pytest` at the top if missing. |

## Alternatives considered

- **Push pid-file write into the `Daemon` class
  (`packages/foreman/src/foreman/daemon.py`).** Rejected: pid files
  are an OS-process concern, not a property of an async runtime.
  Putting the write in `Daemon.start` would force the e2e tests in
  `test_daemon_e2e.py` to invent a pid path and clean it up,
  doubling test surface for no gain. The CLI is the right layer —
  it owns `os.getpid()` semantics anyway.

- **Auto-scan for `foreman.*daemon` processes when the pid file
  is missing (issue option #2).** Rejected: cross-platform process
  scanning needs `psutil` (a new dependency) or platform-specific
  code paths (`Get-CimInstance` on Windows, `ps` on POSIX), and
  the failure mode of "kill the wrong process" is worse DX than
  the clearer diagnostic. The new `daemon stop` message names the
  discovery command for the operator's platform, which gets them
  to the same answer with less risk.

- **Add `psutil` as a dependency for pid liveness checks.**
  Rejected: `os.kill(pid, 0)` already works on POSIX and the
  existing `daemon_status` code at `cli.py:349` already uses it.
  Catching `OSError` alongside `ProcessLookupError` handles the
  Windows edge cases we care about. Don't add a dependency for a
  ~3-line behavior we can get from stdlib.

- **Use `signal.CTRL_BREAK_EVENT` (Windows-specific) for graceful
  shutdown instead of `signal.SIGTERM`.** Rejected: out of scope —
  the daemon's signal handler installation already has a
  `NotImplementedError` swallow on Windows
  (`cli.py:386,389`), so even with the correct signal, graceful
  shutdown on Windows is broken at a deeper layer (foreman#?
  separate ticket if Jeff wants it). The cross-platform "unlink
  pid file after grace period" fallback we add here is the
  correct interim guarantee — the file gets cleaned, even when
  shutdown isn't graceful.

- **Block on `proc.wait()` in `daemon stop` instead of polling the
  pid file.** Rejected: `daemon stop` is launched in a separate
  process from the daemon, so it doesn't have a `subprocess.Popen`
  handle to wait on. Polling the pid file's existence is the
  cross-process equivalent: when the daemon's finally clause runs
  (or our fallback unlinks), the file is gone, and `stop` knows
  shutdown is complete.

- **Make `daemon stop` always unlink the pid file before sending
  SIGTERM (simpler).** Rejected: this races the graceful-shutdown
  path — the daemon's finally clause would silently no-op on
  `missing_ok=True`, but a SECOND concurrent `foreman daemon
  start` could observe the missing pid file mid-stop and start
  spuriously. Removing AFTER the daemon has exited keeps the
  invariant "pid file exists ⇔ daemon should still be running".

- **Make pid path config-only (no fallback when config is
  absent).** Rejected: an operator whose `~/.foreman/config.toml`
  is deleted or corrupted must still be able to `foreman daemon
  stop` a stray daemon. The fallback to the default path costs ~3
  lines of try/except and preserves the recovery affordance.

- **Add a subprocess test on Windows too.** Rejected: Windows
  `subprocess.Popen` without `creationflags=CREATE_NEW_PROCESS_GROUP`
  plus `TerminateProcess` semantics make the test flaky in CI.
  The in-process tests give 95% of the coverage; the subprocess
  test is the literal acceptance criterion from the issue body
  and stays POSIX-only. Cover Windows behavior via the
  `test_daemon_stop_falls_back_unlink_on_timeout` unit test, which
  simulates the Windows "no graceful shutdown" path explicitly.

## Open questions

(none — the bug is reproduced, the root cause is verified by grep
showing zero pid-file writers in the source tree, the fix is
mechanically contained to `cli.py` + one new `DaemonConfig` field,
and every acceptance criterion from the issue body maps to a
specific test.)

## Out of scope

- **Windows graceful shutdown for the daemon.** `os.kill(pid,
  SIGTERM)` on Windows is `TerminateProcess` (hard kill), and the
  daemon's signal handler install at `cli.py:383-389` already
  swallows `NotImplementedError` on Windows. Making the daemon
  shut down gracefully on Windows is a separate concern — likely
  involves a Windows control event, a named pipe, or polling a
  shutdown file. The `_STOP_GRACE_SECONDS` fallback + unlink in
  `daemon stop` keeps the pid file lifecycle correct on Windows
  even without graceful shutdown.

- **Daemonizing (`fork + setsid + detach`) on POSIX.** Today
  `daemon start` runs foreground; making it detach into the
  background is a larger UX change with implications for log
  redirection, working directory, and tty handling. Out of scope
  here — the issue's #4 hypothesis ("daemon start path bypasses
  pid-file write because someone ran without detach") is not
  hypothetical for foreman: there is no detach path, all start
  invocations are foreground, all should write the pid file. If
  Jeff later wants `daemon start --detach`, the helpers added
  here transfer over directly.

- **Multi-daemon / multi-config support.** The `pid_path` config
  field is forward-compatible with running multiple daemons
  (different configs → different pid files), but the rest of the
  daemon (sqlite_path, log_path, state dirs) is also per-config
  already. No need to design multi-daemon coordination here —
  this spec just makes one daemon's pid file work.

- **foreman#44 orchestrator-bot token refresh.** The bug captured
  on 2026-06-02 was masked by the daemon dying after ~1hr due to
  token expiry (foreman#44), so the "stop doesn't work" issue
  was harder to notice in production. Fixing #44 is separate;
  this spec only addresses the start/stop CLI contract.

- **Process-scan fallback in `daemon stop` (issue option #2).**
  We pick the clearer-error path instead (see Alternatives).

- **Replacing `signal.SIGTERM` with a platform-aware shutdown
  signal.** Stays out of scope for the reasons listed under
  alternatives.

- **Updating
  `docs/superpowers/specs/2026-06-01-foreman-daemon-design.md`
  or `foreman-v1-architectural-spec.md` to document the pid-file
  lifecycle.** The behavior is documented by the helper
  docstrings and a comment at the `_write_pid_file` call site;
  the architectural spec doesn't need to track CLI-level OS
  contracts. Fold in next time those files are touched.
