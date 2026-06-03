# Spec: refuse to launch a second daemon when one is already running (issue #88)

## Goal

Make `foreman daemon start` refuse to launch when an existing daemon
is already running, exiting non-zero with a clear message that names
the running daemon's PID. Today the start path
(`packages/foreman/src/foreman/cli.py:311-321`) is unprotected:
back-to-back `foreman daemon start` invocations produce two daemons
both polling the same projects, doubling GitHub API calls and racing
on the same tickets (per-ticket locks are `asyncio.Lock` in
`packages/foreman/src/foreman/locks.py:32-35` — in-process only, no
cross-process protection). The fix is an OS-level exclusive lock on
`~/.foreman/daemon.lock`, which auto-releases on process death so
the daemon's lifecycle stays correct even across crashes, hard
kills, and Windows `TerminateProcess` semantics.

Tracks issue [#88](https://github.com/jeffrichley/foreman/issues/88).
Coordinates with — but does NOT block on —
[#72](https://github.com/jeffrichley/foreman/issues/72) (pid-file
management for `daemon stop`/`status`). The lock file added here and
the pid file added by #72 are orthogonal: the lock file is the
duplicate-detection mutex for `daemon start`; the pid file is the
operator-facing handle for `daemon stop`. Either spec can land first;
both can coexist without coordination.

## Acceptance criteria

- `packages/foreman/src/foreman/config.py`'s `DaemonConfig` gains a
  new `lock_path: str = Field(default="~/.foreman/daemon.lock")`
  field, adjacent to the existing `log_path` and `sqlite_path`
  defaults, so tests and multi-daemon setups can override the path
  via the config file.
- A new module `packages/foreman/src/foreman/daemon_lock.py` provides
  a `DaemonLock` context manager that acquires an OS-level exclusive
  lock on a configurable path (auto-releases on process death) and
  writes the current process's PID into the file's contents. The
  module exposes a single class plus a typed `LockAcquisitionError`
  exception. The class works on POSIX (via `fcntl.flock`) and
  Windows (via `msvcrt.locking`) without conditional imports at
  call sites — one platform check inside the module.
- `daemon_start` in `packages/foreman/src/foreman/cli.py` is wrapped
  in a `DaemonLock` context manager that:
  - On lock acquisition success: writes `os.getpid()` to the file,
    runs the daemon, and releases the lock on exit.
  - On lock acquisition failure: reads the existing file's PID
    content (without holding the lock), raises
    `click.ClickException` so click exits non-zero, with message
    `"Foreman daemon is already running (pid <N>). Use `foreman
    daemon stop` to stop it first."`. When the file content is
    unreadable / not an int, the message falls back to
    `"Foreman daemon is already running (pid: unknown). Use
    `foreman daemon stop` to stop it first."` rather than blowing
    up.
- After a daemon crash (lock file present, but lock holder dead),
  the next `foreman daemon start` succeeds and reuses the file —
  the OS released the lock at the dead process's death, so the
  retry acquires it cleanly. No explicit stale-file cleanup logic
  needed.
- A subprocess-based end-to-end test
  `test_daemon_start_refuses_second_instance` in
  `packages/foreman/tests/test_cli.py` spawns one daemon via
  `subprocess.Popen([sys.executable, "-m", "foreman.cli", "daemon",
  "start"], env=...)` with a tmp-path config that points
  `lock_path` / `sqlite_path` / `log_path` / `FOREMAN_CONFIG` into
  `tmp_path`, polls until the lock file's contents become a valid
  PID matching `proc.pid` (up to 30s), then runs `subprocess.run(
  [..., "daemon", "start"], ...)` with the same env and asserts:
  the second call's `returncode != 0`, its combined output contains
  `"already running"` and the first daemon's PID, and the lock
  file still exists with the first daemon's PID after the second
  call returns. Teardown terminates the first daemon
  (`proc.terminate()` on POSIX, `proc.kill()` on Windows) and
  `proc.wait(timeout=15)`. This test is NOT guarded by
  `skipif Windows`: lock-acquisition semantics must work on both
  platforms (it's the bug Jeff caught on 2026-06-02), and the
  Windows hard-kill teardown is fine since the OS releases the
  lock at process death.
- Unit tests in `packages/foreman/tests/test_daemon_lock.py`:
  - `test_daemon_lock_acquires_and_releases` — acquire via the
    context manager, assert the file exists and contains
    `str(os.getpid())`; assert the file remains on disk after
    release (we don't delete it on release — the OS lock state, not
    the file's existence, is the mutex).
  - `test_daemon_lock_writes_current_pid` — inside the context
    manager body, `Path(lock_path).read_text().strip() ==
    str(os.getpid())`.
  - `test_daemon_lock_creates_parent_directory` — pass
    `tmp_path / "nested" / "deeper" / "daemon.lock"` (parent does
    not exist), acquire, assert success.
  - `test_daemon_lock_raises_when_held_by_another_process` —
    spawn a subprocess that holds the lock (a tiny inline script
    via `sys.executable -c '...'` that acquires the lock and
    sleeps), poll until the subprocess writes its PID to the file,
    then in the test process attempt to acquire the same lock and
    assert `LockAcquisitionError` is raised with the holder PID in
    the message. Teardown kills the subprocess; the OS releases
    the lock at its death.
  - `test_daemon_lock_succeeds_after_crashed_holder` — same setup
    as above but `kill` (SIGKILL on POSIX, `proc.kill()` on
    Windows) the holder subprocess BEFORE attempting acquisition
    in the test process, then `proc.wait()` to confirm death, then
    attempt acquisition and assert success (the lock was released
    at the OS level by the holder's death). This is the
    crash-recovery acceptance criterion.
- Helper tests in `packages/foreman/tests/test_cli.py`:
  - `test_daemon_start_refuses_when_lock_held` — write a fake lock
    file path into the test config, hold the lock from inside the
    test by acquiring it via `DaemonLock(...).__enter__()` (NOT
    using a context manager so we can assert against the lock-held
    state without auto-release), invoke `daemon start
    --max-iterations 1` via `CliRunner`, assert `result.exit_code
    != 0` and `"already running"` is in `result.output` and
    `str(os.getpid())` is in `result.output`. Release the lock in
    test teardown.
  - `test_daemon_start_acquires_lock_and_releases_on_exit` — write
    config with `lock_path`, monkeypatch `_daemon_run` with an
    `async` spy that captures `Path(lock_path).read_text().strip()`
    mid-run, invoke `daemon start --max-iterations 1` via
    `CliRunner`, assert the spy saw the current process's PID in
    the file, and assert a fresh `DaemonLock(lock_path).__enter__()`
    succeeds after the command returns (proving the OS lock was
    released on exit).
  - `test_daemon_start_handles_unreadable_lock_content_gracefully`
    — pre-create the lock file with `"garbage not a pid"`,
    hold the OS lock (via a thread or test fixture that owns the
    lock), invoke `daemon start --max-iterations 1`, assert the
    output contains `"pid: unknown"` and the exit code is non-zero.
- The existing test
  `test_daemon_start_foreground_runs_and_exits_clean`
  (`packages/foreman/tests/test_cli.py:440-456`) is updated to add
  `lock_path = "<tmp_path>/d.lock"` to its test config so the test
  does NOT write to the real user's `~/.foreman/daemon.lock` if a
  real daemon is running on the host. Same hygiene as the
  `sqlite_path` / `log_path` overrides already in that test.
- `packages/foreman/src/foreman/cli.py` must be runnable as a module
  via `python -m foreman.cli daemon start` so the subprocess
  end-to-end test can spawn it without depending on `uv` being on
  PATH. The file currently ends at
  `def main() -> None: cli()` (line 534-536) with no
  `if __name__ == "__main__": main()` guard — add the guard. (If
  the #72 spec lands first and already adds this guard, the change
  is a no-op merge.)
- `just check` exits zero. The new module, the new test file, and
  the cli.py touch all pass ruff + mypy. No new third-party
  dependencies added (stdlib `fcntl` on POSIX, `msvcrt` on Windows).

## Approach

The bug has one mechanical cause: `daemon_start`
(`packages/foreman/src/foreman/cli.py:311-321`) does no liveness
check before calling `asyncio.run(_daemon_run(...))`. Any second
invocation starts a parallel daemon. Per-ticket locks in
`foreman/locks.py` are `asyncio.Lock` — they're per-process only and
don't prevent the cross-process race the bug describes.

The fix is an OS-level exclusive lock on a file. OS file locks have
two properties we want and a pid-file-based check does NOT:

1. **Race-free at acquisition.** Two `daemon start` invocations
   that race to `os.kill(pid, 0)` then `unlink` can both observe
   "no live daemon" if the timing aligns. An OS lock is atomic at
   acquisition — only one of two racing processes succeeds. This
   matters less in single-operator scenarios, but it's free with
   OS locks and not free with pid-file probes.
2. **Auto-released on process death.** The kernel drops the lock
   when the holding process exits — graceful, crashed, OOM-killed,
   `TerminateProcess`d, whatever. We never have to detect and
   clean up a "stale lock" because the OS does it for us. A pid
   file outlives the process and needs a liveness check
   (`os.kill(pid, 0)`) plus cleanup logic; a locked file just
   becomes available.

Stdlib gives us both primitives we need:
- POSIX: `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)` — raises
  `BlockingIOError` if another process holds the lock.
- Windows: `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` — locks one
  byte (the first), raises `OSError` if locked. (One-byte lock is
  sufficient because Windows lock semantics are advisory at the
  byte-range level; we only care about exclusion, not what byte.)

We do NOT add `portalocker` or `filelock` as a dependency. Both are
fine libraries, but the two-platform stdlib primitives we need are
about 20 lines of code; adding a dep for that ratio is not warranted
in a project whose `pyproject.toml` already has 7 prod deps.

The lock file's content carries the holder's PID so the
duplicate-detection branch can name it in the error message. The
flow on a second `daemon start`:

1. Open the lock file (`O_RDWR | O_CREAT`).
2. Try `fcntl.flock(... LOCK_EX | LOCK_NB)` (or msvcrt equivalent).
3. Acquisition fails (`BlockingIOError` / `OSError`).
4. Read the file's existing content — no lock needed for read; the
   holder wrote its PID before we tried to acquire.
5. Parse the content as int. If it parses, the error message names
   the PID. If it doesn't parse (transient: holder wrote nothing
   yet, or wrote garbage), fall back to `"pid: unknown"`.
6. Raise `click.ClickException` so click exits non-zero with the
   message visible.

The lock writer's symmetric flow on a clean `daemon start`:

1. Open the lock file (`O_RDWR | O_CREAT`).
2. Try `fcntl.flock(... LOCK_EX | LOCK_NB)`.
3. Acquisition succeeds.
4. `os.ftruncate(fd, 0)` + write `str(os.getpid())` to the file.
   (Truncate-then-write because the file may contain a previous
   PID from a crashed daemon — we replace it with ours.)
5. KEEP THE FILE DESCRIPTOR OPEN. (Closing the fd releases the
   lock. We hold the fd until daemon exit.)
6. Run the daemon.
7. On exit (graceful or via uncaught exception), close the fd in
   the context manager's `__exit__`. The OS releases the lock at
   close. On crash without `__exit__`, the kernel releases at
   process death — same effect.

This is implemented as a `DaemonLock` context manager in a new
module `foreman/daemon_lock.py`. Putting it in its own module
keeps `cli.py` clean and gives us a clean unit-test surface — the
lock logic is independently testable without spinning up the full
daemon. The module is small (~50 lines including docstrings); no
need to spread it across files.

Why a NEW module instead of putting the lock logic in `foreman/locks.py`?
That file owns per-ticket asyncio.Lock objects (in-process worker
serialization). The daemon-mutex lock is a totally different concern
(cross-process, OS-level, single object, no async). Cohabiting them
in one file just because both are "locks" would muddle the file's
responsibility. Keep them separate.

The `daemon_start` function's new shape:

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
    lock_path = Path(config.daemon.lock_path).expanduser()
    try:
        with DaemonLock(lock_path):
            asyncio.run(_daemon_run(config=config, max_iterations=max_iterations))
    except LockAcquisitionError as exc:
        raise click.ClickException(str(exc)) from exc
```

The `try/except` translation is what makes click exit non-zero with
a clean operator-facing message instead of a Python traceback —
`click.ClickException` is click's documented way to do exit codes
and formatted error output (`sys.exit(1)` plus the message printed
to stderr with the `Error: ` prefix).

Coordination with foreman#72:

- #72 adds pid-file management for the operator-facing
  `daemon stop` / `daemon status` commands. The pid file is at
  `~/.foreman/daemon.pid`.
- This spec adds lock-file management for the duplicate-start
  guard. The lock file is at `~/.foreman/daemon.lock`.
- The two files are independent: `daemon_start` here writes the
  lock; `daemon_start` in #72 writes the pid. If both specs are
  implemented, both files exist when the daemon runs (one as
  mutex, one as operator handle), and the daemon's start path
  does BOTH operations in sequence:
  1. Acquire lock (this spec) — refuses duplicates atomically.
  2. Check + write pid file (#72) — operator handle for stop.
  3. Run daemon.
  4. Clean up pid file (#72) on exit.
  5. Release lock (this spec) on exit.

The merge order is irrelevant. If #88 lands first, `daemon start`
gains duplicate protection but `daemon stop` is still broken (#72's
problem). If #72 lands first, `daemon stop` works but
`daemon start` is still unprotected — both bugs are independent.
The Worker should NOT block this spec's implementation on #72
landing; they're orthogonal.

Why ship the lock file at all if #72's pid-file approach already
includes a liveness probe that raises "already running"? Three
reasons:

1. **Lock release on crash.** The pid file requires a liveness
   probe + stale-file cleanup. The lock file is self-releasing.
   When the daemon crashes mid-run, the pid file's stale-cleanup
   path will eventually unstick it (per #72), but the lock file is
   already unstuck the moment the kernel reaps the dead process.
2. **Race-free at acquisition.** Two simultaneous `daemon start`
   invocations cannot both succeed with the OS lock; with the
   pid-probe approach, a tight enough race window theoretically
   could (though it's unlikely in the operator-typing-in-a-shell
   case the bug is about).
3. **Separation of concerns.** Lock-as-mutex and pid-as-handle are
   different abstractions with different semantics; conflating
   them into one file (whose purpose drifts between mutex and
   handle) makes the daemon lifecycle harder to reason about.

The two-layer strategy from the issue body
("Lock file (primary); PID-file fallback (defense-in-depth)") maps
exactly: this spec is the primary layer, #72 is the defense in
depth. The system is robust if either layer holds.

## Sub-requests (topologically sorted)

1. Add `lock_path` to `DaemonConfig` in
   `packages/foreman/src/foreman/config.py`. Place it adjacent to
   the existing `log_path` and `sqlite_path` defaults (current
   lines 75-77) so the path defaults read as a coherent group:

   ```python
   log_path: str = Field(default="~/.foreman/daemon.log")
   log_level: str = Field(default="INFO")
   sqlite_path: str = Field(default="~/.foreman/foreman.sqlite")
   lock_path: str = Field(default="~/.foreman/daemon.lock")
   ```

   No validator needed — it's a plain path string.

2. Create the new module `packages/foreman/src/foreman/daemon_lock.py`:

   ```python
   """OS-level exclusive lock for the foreman daemon's start-up mutex.

   Used by ``foreman daemon start`` to refuse a second concurrent
   launch (foreman#88). The lock auto-releases on process death,
   so no stale-cleanup logic is needed: a crashed daemon's lock is
   freed the moment the kernel reaps the process.

   Coordinates with foreman#72's pid-file: this lock is the
   primary duplicate-detection mutex; the pid file is the
   operator-facing handle for ``foreman daemon stop``. Both files
   coexist when both specs are implemented; they have different
   purposes and different lifecycles.
   """

   from __future__ import annotations

   import os
   import sys
   from pathlib import Path
   from types import TracebackType


   class LockAcquisitionError(RuntimeError):
       """Raised when the daemon lock is already held by another process."""


   class DaemonLock:
       """Context manager that holds an OS exclusive lock on a file.

       Usage:
           with DaemonLock(path):
               run_daemon()

       On enter: opens ``path`` (creating it and parent dirs if
       needed), acquires an exclusive non-blocking OS lock, and
       writes ``str(os.getpid())`` to the file's contents.

       On exit (or process death): the OS releases the lock.

       On lock-already-held: raises ``LockAcquisitionError`` with a
       message naming the holder's PID (read from the file's
       contents, or "unknown" if the file is unreadable).
       """

       def __init__(self, path: Path | str) -> None:
           self._path = Path(path).expanduser()
           self._fd: int | None = None

       def __enter__(self) -> DaemonLock:
           self._path.parent.mkdir(parents=True, exist_ok=True)
           # O_CREAT so the file appears on first run; O_RDWR so we
           # can both write our PID into it AND read the holder's
           # PID from it on a failed acquisition (read does not
           # need the lock).
           fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
           try:
               _acquire_exclusive_nonblocking(fd)
           except (BlockingIOError, OSError) as exc:
               os.close(fd)
               holder_pid = _read_holder_pid(self._path)
               raise LockAcquisitionError(
                   _format_already_running_message(holder_pid)
               ) from exc
           # Acquired. Truncate any prior PID and write ours.
           os.ftruncate(fd, 0)
           os.write(fd, str(os.getpid()).encode("ascii"))
           os.fsync(fd)  # so other processes reading our PID see it
           self._fd = fd
           return self

       def __exit__(
           self,
           exc_type: type[BaseException] | None,
           exc: BaseException | None,
           tb: TracebackType | None,
       ) -> None:
           if self._fd is not None:
               # Closing the fd releases the OS lock. We intentionally
               # do NOT delete the file — its existence isn't the mutex;
               # the OS lock is.
               os.close(self._fd)
               self._fd = None


   def _acquire_exclusive_nonblocking(fd: int) -> None:
       """Try-once exclusive lock on ``fd``. Raises on contention."""
       if sys.platform == "win32":
           import msvcrt

           # Lock 1 byte at the current file position (0). Windows
           # locking is byte-range-based; one byte is sufficient
           # exclusion for our single-mutex use case.
           msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
       else:
           import fcntl

           fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


   def _read_holder_pid(path: Path) -> int | None:
       """Best-effort: parse the PID written by the lock holder.

       Returns ``None`` if the file is unreadable or its content
       doesn't parse as an int (transient: holder wrote nothing
       yet, or wrote garbage).
       """
       try:
           text = path.read_text(encoding="ascii").strip()
       except OSError:
           return None
       try:
           return int(text)
       except ValueError:
           return None


   def _format_already_running_message(pid: int | None) -> str:
       pid_part = str(pid) if pid is not None else "unknown"
       return (
           f"Foreman daemon is already running (pid {pid_part}). "
           f"Use `foreman daemon stop` to stop it first."
       )
   ```

3. Update `daemon_start` in
   `packages/foreman/src/foreman/cli.py` (currently lines 311-321)
   to wrap the asyncio run in a `DaemonLock` context manager:

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
       from foreman.daemon_lock import DaemonLock, LockAcquisitionError

       config = _load_config_from_env()
       lock_path = Path(config.daemon.lock_path).expanduser()
       try:
           with DaemonLock(lock_path):
               asyncio.run(
                   _daemon_run(config=config, max_iterations=max_iterations)
               )
       except LockAcquisitionError as exc:
           raise click.ClickException(str(exc)) from exc
   ```

   The import is inside the function (not module-level) so the
   stdlib platform branch doesn't get imported when other CLI
   commands are invoked. Module-level is also acceptable; either
   is fine — pick whichever ruff is happiest with.

4. Add a module-runnable guard to the bottom of
   `packages/foreman/src/foreman/cli.py` so the subprocess test
   in sub-request 7 can run `python -m foreman.cli daemon start`.
   Currently the file ends at line 537 with
   `def main() -> None: cli()`. Append:

   ```python


   if __name__ == "__main__":
       main()
   ```

   If foreman#72 lands first and has already added this guard,
   this sub-request is a no-op verify step. (Same guard, same
   place.)

5. Update the existing test
   `test_daemon_start_foreground_runs_and_exits_clean` in
   `packages/foreman/tests/test_cli.py` (currently lines 440-456)
   to set `lock_path` in the test config so the test does not
   write to the real user's `~/.foreman/daemon.lock`. Replace the
   `config_path.write_text` block with:

   ```python
   config_path.write_text(
       f'[admin]\ngithub_token_env = "X"\n'
       f'[daemon]\n'
       f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
       f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
       f'lock_path = "{(tmp_path / "d.lock").as_posix()}"\n'
   )
   ```

6. Create `packages/foreman/tests/test_daemon_lock.py`:

   ```python
   """Unit tests for DaemonLock (foreman#88)."""

   from __future__ import annotations

   import os
   import subprocess
   import sys
   import time
   from pathlib import Path

   import pytest

   from foreman.daemon_lock import DaemonLock, LockAcquisitionError


   def test_daemon_lock_acquires_and_releases(tmp_path: Path) -> None:
       lock_path = tmp_path / "d.lock"
       with DaemonLock(lock_path):
           assert lock_path.exists()
       # File remains after release; the OS lock state, not the
       # file's existence, is the mutex.
       assert lock_path.exists()


   def test_daemon_lock_writes_current_pid(tmp_path: Path) -> None:
       lock_path = tmp_path / "d.lock"
       with DaemonLock(lock_path):
           assert lock_path.read_text(encoding="ascii").strip() == str(os.getpid())


   def test_daemon_lock_creates_parent_directory(tmp_path: Path) -> None:
       lock_path = tmp_path / "nested" / "deeper" / "d.lock"
       with DaemonLock(lock_path):
           assert lock_path.exists()
           assert lock_path.parent.is_dir()


   _HOLDER_SCRIPT = """\
   import os, sys, time
   from foreman.daemon_lock import DaemonLock
   lock_path = sys.argv[1]
   with DaemonLock(lock_path):
       sys.stdout.write("locked\\n")
       sys.stdout.flush()
       time.sleep(30)
   """


   def _spawn_holder(lock_path: Path) -> subprocess.Popen[str]:
       proc = subprocess.Popen(
           [sys.executable, "-c", _HOLDER_SCRIPT, str(lock_path)],
           stdout=subprocess.PIPE,
           stderr=subprocess.PIPE,
           text=True,
           env={**os.environ},
       )
       # Wait for the holder to print "locked" so we know it has the lock
       # AND has written its PID to the file.
       assert proc.stdout is not None
       deadline = time.monotonic() + 15
       while time.monotonic() < deadline:
           line = proc.stdout.readline()
           if line.strip() == "locked":
               return proc
           if proc.poll() is not None:
               raise RuntimeError(
                   f"Holder exited prematurely: rc={proc.returncode}, "
                   f"stderr={proc.stderr.read() if proc.stderr else ''}"
               )
       proc.kill()
       raise TimeoutError("Holder did not acquire lock within 15s")


   def test_daemon_lock_raises_when_held_by_another_process(
       tmp_path: Path,
   ) -> None:
       lock_path = tmp_path / "d.lock"
       holder = _spawn_holder(lock_path)
       try:
           with pytest.raises(LockAcquisitionError) as excinfo:
               with DaemonLock(lock_path):
                   pass
           assert "already running" in str(excinfo.value)
           assert str(holder.pid) in str(excinfo.value)
       finally:
           holder.kill()
           holder.wait(timeout=10)


   def test_daemon_lock_succeeds_after_crashed_holder(
       tmp_path: Path,
   ) -> None:
       """A crashed daemon's lock is released by the OS at process
       death; the next start succeeds without manual cleanup."""
       lock_path = tmp_path / "d.lock"
       holder = _spawn_holder(lock_path)
       holder.kill()
       holder.wait(timeout=10)
       # Holder is dead; OS released the lock.
       with DaemonLock(lock_path):
           assert lock_path.read_text(encoding="ascii").strip() == str(os.getpid())
   ```

7. Add the integration test
   `test_daemon_start_refuses_second_instance` to
   `packages/foreman/tests/test_cli.py`. Place it next to the
   existing daemon tests (`test_daemon_start_foreground_runs_and_exits_clean`,
   currently line 440):

   ```python
   def test_daemon_start_refuses_second_instance(tmp_path: Path) -> None:
       """End-to-end: two `foreman daemon start` invocations in
       parallel — the second exits non-zero with a clear message
       naming the first's PID (foreman#88 issue-body acceptance)."""
       import subprocess
       import sys
       import time

       lock_path = tmp_path / "d.lock"
       config_path = tmp_path / "config.toml"
       config_path.write_text(
           f'[admin]\ngithub_token_env = "X"\n'
           f'[daemon]\n'
           f'lock_path = "{lock_path.as_posix()}"\n'
           f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
           f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
       )
       env = {**os.environ, "FOREMAN_CONFIG": str(config_path)}

       proc = subprocess.Popen(
           [sys.executable, "-m", "foreman.cli", "daemon", "start"],
           env=env,
       )
       try:
           # Poll until the first daemon has acquired the lock AND
           # written its PID into the file.
           deadline = time.monotonic() + 30
           while time.monotonic() < deadline:
               if lock_path.exists():
                   content = lock_path.read_text(encoding="ascii").strip()
                   if content and content.isdigit() and int(content) == proc.pid:
                       break
               time.sleep(0.1)
           else:
               raise AssertionError(
                   "First daemon did not write lock-file PID within 30s"
               )

           # Now try to start a second daemon with the same config.
           result = subprocess.run(
               [
                   sys.executable, "-m", "foreman.cli",
                   "daemon", "start",
               ],
               env=env,
               capture_output=True,
               text=True,
               timeout=20,
           )
           combined = (result.stdout or "") + (result.stderr or "")
           assert result.returncode != 0, (
               f"Second daemon start should exit non-zero. "
               f"output: {combined}"
           )
           assert "already running" in combined, combined
           assert str(proc.pid) in combined, combined

           # Lock file is unchanged.
           assert lock_path.read_text(encoding="ascii").strip() == str(proc.pid)
       finally:
           # Cross-platform teardown: terminate the first daemon.
           # On Windows, this is TerminateProcess (hard kill) — the
           # OS releases the lock at process death regardless.
           if proc.poll() is None:
               proc.terminate() if sys.platform != "win32" else proc.kill()
               try:
                   proc.wait(timeout=15)
               except subprocess.TimeoutExpired:
                   proc.kill()
                   proc.wait(timeout=5)
   ```

   Add `import os`, `import sys`, `import pytest` to the top of
   `test_cli.py` if not already present.

8. Add the CLI-level unit tests to
   `packages/foreman/tests/test_cli.py`:

   ```python
   def test_daemon_start_refuses_when_lock_held(
       tmp_path: Path, monkeypatch
   ) -> None:
       """daemon_start exits non-zero when the lock is held by
       another process — same-process variant exercises the
       cli.py error-translation path without needing subprocess."""
       from foreman.daemon_lock import DaemonLock

       lock_path = tmp_path / "d.lock"
       config_path = tmp_path / "config.toml"
       config_path.write_text(
           f'[admin]\ngithub_token_env = "X"\n'
           f'[daemon]\n'
           f'lock_path = "{lock_path.as_posix()}"\n'
           f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
           f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
       )
       monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

       # Hold the lock outside of a `with` block so the test owns
       # release timing.
       holder = DaemonLock(lock_path).__enter__()
       try:
           result = CliRunner().invoke(
               cli, ["daemon", "start", "--max-iterations", "1"]
           )
           assert result.exit_code != 0
           assert "already running" in result.output
           assert str(os.getpid()) in result.output
       finally:
           holder.__exit__(None, None, None)


   def test_daemon_start_acquires_lock_and_releases_on_exit(
       tmp_path: Path, monkeypatch
   ) -> None:
       """daemon_start holds the lock during the run and releases
       it on exit — verified by a fresh acquisition after the
       command returns."""
       from foreman.daemon_lock import DaemonLock

       lock_path = tmp_path / "d.lock"
       config_path = tmp_path / "config.toml"
       config_path.write_text(
           f'[admin]\ngithub_token_env = "X"\n'
           f'[daemon]\n'
           f'lock_path = "{lock_path.as_posix()}"\n'
           f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
           f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
       )
       monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

       captured: dict[str, str | None] = {}

       async def spy(*, config, max_iterations):  # noqa: ARG001
           captured["mid_run_pid"] = (
               lock_path.read_text(encoding="ascii").strip()
               if lock_path.exists()
               else None
           )

       monkeypatch.setattr("foreman.cli._daemon_run", spy)

       result = CliRunner().invoke(
           cli, ["daemon", "start", "--max-iterations", "1"]
       )
       assert result.exit_code == 0, result.output
       assert captured["mid_run_pid"] == str(os.getpid())

       # Fresh acquisition must succeed (lock was released on exit).
       with DaemonLock(lock_path):
           pass  # If this raises, the previous run leaked the lock.


   def test_daemon_start_handles_unreadable_lock_content_gracefully(
       tmp_path: Path, monkeypatch
   ) -> None:
       """When the lock holder hasn't written a valid PID yet, the
       error message falls back to 'pid: unknown' instead of
       crashing."""
       from foreman.daemon_lock import (
           DaemonLock,
           LockAcquisitionError,
           _format_already_running_message,
       )

       lock_path = tmp_path / "d.lock"
       # Write garbage content before acquiring the lock.
       lock_path.write_text("not a pid")

       config_path = tmp_path / "config.toml"
       config_path.write_text(
           f'[admin]\ngithub_token_env = "X"\n'
           f'[daemon]\n'
           f'lock_path = "{lock_path.as_posix()}"\n'
           f'sqlite_path = "{(tmp_path / "f.sqlite").as_posix()}"\n'
           f'log_path = "{(tmp_path / "d.log").as_posix()}"\n'
       )
       monkeypatch.setenv("FOREMAN_CONFIG", str(config_path))

       # Acquire the lock and IMMEDIATELY re-truncate to "garbage"
       # so the second start sees unparseable content. We bypass
       # the DaemonLock context manager's normal PID write to
       # simulate this edge case.
       import sys as _sys

       fd = os.open(lock_path, os.O_RDWR)
       try:
           if _sys.platform == "win32":
               import msvcrt
               msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
           else:
               import fcntl
               fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
           os.ftruncate(fd, 0)
           os.write(fd, b"not a pid")
           os.fsync(fd)

           result = CliRunner().invoke(
               cli, ["daemon", "start", "--max-iterations", "1"]
           )
           assert result.exit_code != 0
           assert "pid unknown" in result.output or "pid: unknown" in result.output
       finally:
           os.close(fd)
   ```

9. Run targeted tests first to confirm the new module + tests pass:

   ```bash
   uv run pytest packages/foreman/tests/test_daemon_lock.py packages/foreman/tests/test_cli.py -k "lock or daemon_start" -v
   ```

10. Run `just check` and confirm exit zero.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/config.py` | Add `lock_path: str = Field(default="~/.foreman/daemon.lock")` to `DaemonConfig`, adjacent to the existing `log_path` / `sqlite_path` defaults. No validator. |
| `packages/foreman/src/foreman/daemon_lock.py` | New module. Defines `LockAcquisitionError` exception and `DaemonLock` context manager. Internal helpers `_acquire_exclusive_nonblocking` (POSIX/Windows platform branch), `_read_holder_pid`, `_format_already_running_message`. ~80 lines including docstrings. No third-party deps. |
| `packages/foreman/src/foreman/cli.py` | Wrap `daemon_start`'s asyncio.run in `DaemonLock(lock_path)` context manager. Translate `LockAcquisitionError` into `click.ClickException` so click exits non-zero with the operator-facing message. Add `if __name__ == "__main__": main()` guard at the bottom (no-op merge if foreman#72 lands first). |
| `packages/foreman/tests/test_daemon_lock.py` | New test file. Five unit tests: `test_daemon_lock_acquires_and_releases`, `test_daemon_lock_writes_current_pid`, `test_daemon_lock_creates_parent_directory`, `test_daemon_lock_raises_when_held_by_another_process` (spawns a subprocess holder), `test_daemon_lock_succeeds_after_crashed_holder` (kills the holder and asserts re-acquisition works). |
| `packages/foreman/tests/test_cli.py` | Add `lock_path` to the existing `test_daemon_start_foreground_runs_and_exits_clean` config. Add four new tests: `test_daemon_start_refuses_when_lock_held` (in-process CliRunner), `test_daemon_start_acquires_lock_and_releases_on_exit` (mid-run spy + post-run re-acquisition), `test_daemon_start_handles_unreadable_lock_content_gracefully` (garbage content), `test_daemon_start_refuses_second_instance` (subprocess end-to-end, cross-platform). Add `import os`, `import sys`, `import pytest` at top if missing. |

## Alternatives considered

- **Push the duplicate check into `Daemon.start()`
  (`packages/foreman/src/foreman/daemon.py:52-65`) instead of
  wrapping `daemon_start` in `cli.py`.** Rejected: a process-mutex
  lock is an OS/CLI concern, not a property of the daemon's async
  runtime. The existing `Daemon` class is unit-tested via
  `test_daemon_e2e.py` with no notion of process identity; pushing
  the lock into `Daemon.start()` would force those tests to invent
  lock paths and clean them up, doubling test surface. The CLI is
  the right layer because it owns `os.getpid()` semantics anyway.

- **Use the pid file from foreman#72 as the mutex; don't add a
  separate lock file.** Rejected: the pid-file-based approach has
  two weaknesses the OS lock avoids — (a) it needs explicit
  liveness probes + stale-file cleanup (the lock auto-releases on
  process death), and (b) two racing `daemon start` invocations
  could in principle both observe "no live daemon" if the timing
  aligns (the OS lock is atomic at acquisition). The issue body
  explicitly says "Lock file (primary), PID-file fallback
  (defense-in-depth)" — this spec implements the primary layer;
  #72 is the secondary layer. Both layers are useful for different
  reasons (this one: race-free mutex; #72: operator handle for
  `daemon stop`). Conflating them is worse engineering.

- **Add `portalocker` (or `filelock`) as a third-party dependency
  for cross-platform locking.** Rejected: the stdlib primitives
  (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows) cover our
  exact use case in ~10 lines of code. Adding a dep for that
  ratio in a project whose `pyproject.toml` already has 7 prod
  deps is unjustified. `portalocker` is a fine library but not
  necessary here.

- **Use `signal.SIGUSR1` to "ping" the existing daemon and let it
  reply.** Rejected: out of scope and adds significant complexity
  (signal handlers, response channel). The lock-file approach is
  the standard pattern for single-instance enforcement and works
  on Windows where SIGUSR1 doesn't exist.

- **Scan the process table for `foreman.*daemon` patterns and
  refuse if any match.** Rejected: cross-platform process-table
  scanning requires `psutil` (new dep) or platform-specific code
  (`Get-CimInstance` on Windows, `ps` on POSIX). It also has a
  bad false-positive mode: a developer running
  `python -m foreman.cli daemon start` in a debugger from another
  terminal would block the operator's daemon start. The lock file
  is config-scoped — each lock-path corresponds to one daemon
  identity, no false positives across debug sessions or multiple
  configs.

- **Make the lock file path env-var-overridable
  (`FOREMAN_LOCK_PATH`) like `FOREMAN_CONFIG`.** Rejected: the
  existing per-field paths (`log_path`, `sqlite_path`) are
  config-file-only — the env-var override pattern is reserved for
  the config-file path itself and the worktrees-root. Adding
  `FOREMAN_LOCK_PATH` would be a one-off inconsistency. If
  operators need env-var-driven daemon paths, that's a separate
  cleanup that touches all the path fields uniformly. Tests
  already use config-file overrides via `FOREMAN_CONFIG=<tmp>`,
  so the test surface is fine.

- **Delete the lock file on context-manager exit.** Rejected:
  deleting the file races a concurrent `daemon start` that opens
  the file and then sees it disappear. The OS lock state, not the
  file's existence, is the mutex; the file persists across daemon
  lifecycles and gets overwritten on the next start. Same pattern
  as classic unix lockfiles.

## Open questions

(none — the bug is reproduced from the issue body, the fix is a
~80-line new module plus ~10 lines of cli.py changes, all
acceptance criteria from the issue body map to specific tests, and
the spec coordinates cleanly with foreman#72 without depending on
it.)

## Out of scope

- **Fixing `foreman daemon stop`.** That's foreman#72; the spec
  for it already exists at `docs/superpowers/specs/foreman-issue-72-spec.md`.
  This spec adds the start-side mutex; #72 fixes the stop-side
  pid-file handling. The two are orthogonal — both can land
  independently. Implementing this spec does NOT close #72.

- **Cross-machine multi-daemon scenarios (NFS-mounted state dir,
  shared SQLite, etc.).** OS file locks have well-known behavior
  on NFS that varies by implementation; cross-host coordination
  needs a different primitive (e.g., a SQL row-level lock in the
  daemon's storage). Foreman's documented use is single-machine
  per operator — out of scope.

- **Graceful migration of in-flight tickets between daemon
  instances.** When `daemon start` refuses because another daemon
  is running, this spec does not attempt to take over its work or
  drain its queue. The operator stops the old daemon and starts a
  new one; in-flight tickets are handled by the daemon's
  reconciliation pass on the next start
  (`Daemon._reconcile_in_flight`,
  `packages/foreman/src/foreman/daemon.py:67-105`).

- **Multi-daemon support on a single machine.** The `lock_path`
  field is forward-compatible with running multiple daemons
  (different config files → different lock paths), and the rest
  of the daemon (sqlite_path, log_path) is already per-config.
  But the orchestrator-bot identity, GitHub App registration, and
  queue coordination aren't designed for that yet. Multi-daemon
  is a v2 feature; this spec doesn't preclude it.

- **Env-var override `FOREMAN_LOCK_PATH`.** See alternatives —
  consistency with `log_path` / `sqlite_path` (which are
  config-file-only) wins. If env-var overrides land, they should
  land uniformly across all daemon path fields in a separate
  spec.

- **Documenting the lock file in
  `docs/superpowers/specs/foreman-v1-architectural-spec.md` or
  `2026-06-01-foreman-daemon-design.md`.** The behavior is
  documented by the new module's docstring and the
  `daemon_start` comment. The architectural spec doesn't track
  CLI-level OS contracts; fold in next time those files are
  touched.

- **Replacing `fcntl.flock` with `fcntl.lockf`** (POSIX advisory
  vs. process-bound locks). `flock` is the standard choice for
  whole-file process mutex and is what's used by every reference
  implementation of single-instance daemons. `lockf` adds
  byte-range semantics we don't need. Stick with `flock`.
