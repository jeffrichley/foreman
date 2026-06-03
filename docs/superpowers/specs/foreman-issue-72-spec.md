# Spec: make `foreman daemon stop` work by reusing the lock file as the PID source (issue #72)

## Goal

Fix the `foreman daemon` CLI so `daemon stop` and `daemon status` can
actually see a running daemon. Today neither command works: `daemon
start` (`packages/foreman/src/foreman/cli.py:317-329`) launches the
async daemon under `DaemonLock` (from foreman#88) but the stop and
status commands look for `~/.foreman/daemon.pid` — a file no part of
the codebase writes. Result: a daemon that has been running overnight
reports as "not running" and cannot be stopped without finding its
PID via `Get-CimInstance Win32_Process` (caught 2026-06-02 on Jeff's
Windows host).

Tracks issue [#72](https://github.com/jeffrichley/foreman/issues/72).

## Acceptance criteria

- `daemon stop` reads the daemon's PID from the **lock file**
  (`~/.foreman/daemon.lock`, configurable via `DaemonConfig.lock_path`
  and `FOREMAN_LOCK_PATH` env var). `DaemonLock.__enter__` already
  writes `str(os.getpid())` to that file on acquisition
  (`packages/foreman/src/foreman/daemon_lock.py:55-67`), so the
  acquisition contract already includes a PID-as-content invariant.
  Reusing it eliminates the need for a separate pid file.
- A new module-private helper
  `_resolve_lock_path(config: Config | None) -> Path` in `cli.py`
  returns the lock path with this precedence: `FOREMAN_LOCK_PATH`
  env var, then `config.daemon.lock_path`, then the default
  `~/.foreman/daemon.lock`. `daemon_start`'s existing env-override
  logic moves into this helper so all three subcommands resolve the
  same path identically.
- A new module-private helper
  `_read_lock_file_pid(lock_path: Path) -> int | None` in `cli.py`
  returns the integer PID written by `DaemonLock`, or `None` if the
  file is missing, unreadable, or its content is not a base-10 int
  (treat all three as "no addressable daemon"). Callers branch on
  `None` to print a clear diagnostic rather than crash.
- `daemon stop` (`cli.py:324-337`):
  - Resolves the lock path via `_resolve_lock_path`, falling back to
    the no-config default if config loading raises (so the operator
    can stop the daemon from a host where the config file has been
    removed).
  - If the lock file is missing, emits a diagnostic that names the
    resolved path AND a platform-appropriate discovery command:
    `"No daemon lock file at <path>. Either the daemon was never
    started, or the lock file was removed. To find a stray process:
    \`tasklist | findstr foreman\` (Windows) or \`ps aux | grep
    foreman\` (POSIX), then kill the PID directly."`.
  - If the lock file is present but unreadable, emits a clear
    message and does NOT send a signal to any PID.
  - If the lock file is present and parseable, calls
    `os.kill(pid, signal.SIGTERM)`. On `ProcessLookupError` (the
    PID is already dead), reports the stale state and exits 0.
  - After SIGTERM, polls process liveness with `os.kill(pid, 0)`
    every `_STOP_POLL_INTERVAL_SECONDS` (default 100 ms) for up to
    `_STOP_GRACE_SECONDS` (default 10 s). When the probe raises
    `ProcessLookupError`, prints `"Daemon stopped cleanly."`. If
    the process is still alive after the grace period, prints a
    diagnostic naming the daemon's PID — the lock file is NOT
    forcibly removed by `stop`, because the OS releases the lock
    on process death regardless of whether the file is unlinked.
- `daemon status` (`cli.py:340-352`) resolves the lock path via the
  same helper. If the file is missing, prints "Daemon: not running."
  If present, reads the PID, probes liveness via `os.kill(pid, 0)`,
  and prints either `"Daemon: running (pid <N>)."` or
  `"Daemon: stale lock file (pid <N> dead). The OS released the
  lock; the next `foreman daemon start` will overwrite the file."`.
- `daemon start` itself needs **no change** in its
  pid/lock-handling logic beyond what foreman#88 already shipped.
  `DaemonLock.__enter__` continues to write `str(os.getpid())` to
  the lock file on acquisition. The new daemon_stop / daemon_status
  read from that same file. **One file, one role per lifecycle
  phase.**
- `packages/foreman/src/foreman/config.py`'s `DaemonConfig`:
  - Keeps the existing `lock_path: str = Field(default=
    "~/.foreman/daemon.lock")` field from foreman#88.
  - Does NOT add a separate `pid_path` field. The lock file IS the
    pid file. A separate field would re-introduce the two-file
    lifecycle that caused CI Windows hangs in the first revision of
    this spec.
- The existing #88 tests
  (`test_daemon_start_refuses_when_lock_held`, …,
  `test_daemon_start_refuses_second_instance`) require no change —
  they already pin `lock_path` to `tmp_path` and exercise the
  acquisition contract end-to-end.
- New tests in `packages/foreman/tests/test_cli.py`:
  - Helper-level tests for `_read_lock_file_pid` (missing file,
    corrupt content, valid content) and `_resolve_lock_path`
    (env-var override, no-config fallback).
  - `test_daemon_stop_reads_lock_file_pid_and_sends_sigterm` —
    write `os.getpid()` to a tmp lock file, monkeypatch `os.kill`
    to capture the SIGTERM and raise `ProcessLookupError` on the
    sig=0 liveness probe (simulating clean exit), assert `stop`
    exits 0 with `"Daemon stopped cleanly."`.
  - `test_daemon_stop_reports_when_daemon_does_not_exit` —
    write `os.getpid()` to lock, monkeypatch `os.kill` to no-op
    (process refuses to die), shrink the grace period, assert
    `stop` reports the timeout AND **does NOT remove the lock
    file** (that's the dying daemon's OS-level contract, not ours).
  - `test_daemon_stop_with_missing_lock_file_gives_actionable_message` —
    no lock file, assert exit 0, the message includes the resolved
    path, the word `foreman`, and one of `tasklist` / `ps aux`.
  - `test_daemon_stop_with_dead_pid_reports_stale` — write a known-
    dead PID, monkeypatch `os.kill` to raise `ProcessLookupError`,
    assert `stop` reports the stale state without unlinking.
  - `test_daemon_stop_with_unreadable_lock_content_reports` — write
    garbage to the lock file, monkeypatch `os.kill` to capture
    calls, assert `stop` reports the unreadable content AND never
    attempted to signal any PID.
  - `test_daemon_stop_works_without_config_file` — no
    `FOREMAN_CONFIG`, override `FOREMAN_LOCK_PATH` to a tmp lock,
    write a PID to it, monkeypatch `os.kill`, assert `stop` exits 0.
  - `test_daemon_status_reports_running_when_lock_pid_alive` —
    write `os.getpid()` to lock, assert status reports running.
  - `test_daemon_status_reports_stale_when_lock_pid_dead` — dead
    PID + monkeypatched kill that raises, assert "stale" in output.
- A subprocess-based end-to-end test `test_daemon_start_stop_subprocess`
  (no platform skip): spawns `foreman daemon start` as a real
  subprocess with a tmp lock path, polls the lock file for its PID
  content, runs `foreman daemon stop`, asserts the stop output,
  waits for `proc.wait`, and **verifies the OS lock is released**
  by acquiring it via `DaemonLock` in the test process. This last
  step is the architectural contract — even if the lock file
  remains on disk, the OS must allow re-acquisition because the
  daemon process is gone.
- `packages/foreman/src/foreman/cli.py` must remain runnable as a
  module via `python -m foreman.cli` for the subprocess test. The
  file already has `if __name__ == "__main__": main()` at the
  bottom (from foreman#88's merge), no change needed.
- `just check` exits zero. The change touches only `cli.py`,
  `config.py` (no-op — lock_path field already exists), and
  `tests/test_cli.py`; existing daemon-class and poller-class tests
  must continue to pass.

## Approach

The bug has one cause: nothing writes to the file `daemon stop` was
looking for. But foreman#88 already shipped a file that contains
exactly the data `daemon stop` needs — the daemon's PID — and it's
written by the daemon as part of its lock-acquisition contract.
The fix is to read from that file instead of looking for a separate
pid file.

This is a structural improvement, not just a path-rename. Two
separate files (lock + pid) would mean two stale-file lifecycles,
two failure modes, and two places to keep in sync. A single file
that the OS lock holds open while the daemon runs and that contains
the daemon's PID for the stop tool to read is one lifecycle and one
failure mode. We pay no additional cost beyond what foreman#88
already incurred.

**File semantics at the contract level:**

| State | File on disk | OS lock | Content |
|---|---|---|---|
| Daemon running | exists | held by daemon | daemon's PID |
| Daemon exited cleanly | exists | free | daemon's PID (now stale) |
| Daemon hard-killed | exists | free | daemon's PID (now stale) |
| No daemon ever started | absent | — | — |
| Pre-start by daemon mid-acquisition | exists | held by daemon | garbage / empty (transient) |

`daemon stop`'s job: read the PID, signal it, poll for death.
`daemon start`'s job (already shipped via foreman#88): acquire the
lock, write your PID, run. `daemon status`'s job: read the PID, probe
liveness, report.

`daemon stop`'s new shape needs to handle four cases:

- **Lock file present, PID alive** (happy path): SIGTERM, poll for
  process death up to `_STOP_GRACE_SECONDS`, report cleanly. On
  POSIX, SIGTERM lets the daemon run its signal handler + cleanup.
  On Windows, `os.kill(pid, SIGTERM)` is `TerminateProcess` (hard
  kill) — the daemon dies near-instantly. Either way, the OS
  releases the lock when the daemon process dies.
- **Lock file present, PID dead** (post-crash): report stale, exit
  0. We don't unlink — the file isn't blocking anything. The next
  `daemon start` will acquire the lock (OS-level — the file
  existing isn't the mutex) and overwrite the PID content.
- **Lock file present, content unreadable** (transient — daemon
  mid-acquisition, OR an external process corrupted the file): report
  clearly, don't signal anything, exit 0.
- **Lock file absent** (the bug scenario the issue raised): emit a
  diagnostic that names both the resolved path AND the
  platform-appropriate process-discovery command so the operator can
  find a stray daemon process without us scanning for them.

Why poll `os.kill(pid, 0)` instead of polling the file's existence?
Because in the merged-file design, the file's existence is
decoupled from the daemon's liveness. The file exists from the
moment `DaemonLock` opens it and stays until the next `daemon
start` truncates+rewrites it. Process liveness is the actual signal
we want.

Why not unlink the file as a `stop`-side cleanup? Because the file
existing isn't the lock. The OS lock is held by an open file
descriptor inside the daemon process; the file on disk is just a
PID record. Unlinking it before the daemon dies would race a
concurrent `daemon start` (it'd recreate the file and acquire its
own lock while the original daemon is still terminating); after the
daemon dies the file is stale but harmless and the next start
naturally overwrites it. Leaving it alone keeps the invariant
"lock file content is the most recent acquirer's PID" simple to
reason about.

The new `_resolve_lock_path` helper centralizes the
`FOREMAN_LOCK_PATH > config.daemon.lock_path > default` precedence
so daemon_start, daemon_stop, and daemon_status agree on which file
to look at. foreman#88's `daemon_start` had the resolution inline;
extracting it into a helper is a small refactor that pays for
itself the first time someone forgets to keep the three subcommands
in sync.

## Sub-requests (topologically sorted)

1. Add `_resolve_lock_path(config: Config | None) -> Path` and
   `_read_lock_file_pid(lock_path: Path) -> int | None` to
   `packages/foreman/src/foreman/cli.py`. Place them in the
   daemon-helpers section (above the `@cli.group()` for `daemon`),
   alongside `_STOP_GRACE_SECONDS` / `_STOP_POLL_INTERVAL_SECONDS`
   module constants.

2. Refactor `daemon_start` to use `_resolve_lock_path` instead of
   the inline env-override logic. Body becomes:

   ```python
   def daemon_start(max_iterations: int | None) -> None:
       from foreman.daemon_lock import DaemonLock, LockAcquisitionError

       config = _load_config_from_env()
       lock_path = _resolve_lock_path(config)
       try:
           with DaemonLock(lock_path):
               asyncio.run(_daemon_run(config=config, max_iterations=max_iterations))
       except LockAcquisitionError as exc:
           raise click.ClickException(str(exc)) from exc
   ```

3. Rewrite `daemon_stop` per the four-case branching described in
   the Approach. Use `_read_lock_file_pid` to parse, poll liveness
   via `os.kill(pid, 0)` (catching `ProcessLookupError`/`OSError`),
   and emit clear diagnostic messages. Do not unlink the file from
   inside `stop`.

4. Rewrite `daemon_status` to use `_resolve_lock_path` +
   `_read_lock_file_pid` symmetrically with `daemon_stop`.

5. Update `test_daemon_status_when_not_running` and
   `test_daemon_start_foreground_runs_and_exits_clean` (the
   pre-existing #88 tests) so their tmp configs set `lock_path` —
   not `pid_path` (no such field exists in the lock-file design).

6. Add the helper unit tests (`_read_lock_file_pid` × 3,
   `_resolve_lock_path` × 2).

7. Add the `daemon_stop` unit tests (six tests covering: SIGTERM
   + clean exit, no-exit-within-timeout, missing lock file, dead
   PID, unreadable content, no-config fallback).

8. Add the `daemon_status` unit tests (running, stale).

9. Add the subprocess end-to-end test
   `test_daemon_start_stop_subprocess` with `DaemonLock`
   re-acquisition as the lock-release verification.

10. Run targeted tests: `uv run --no-sync pytest packages/foreman/tests/test_cli.py -v`.

11. Run `just check` and confirm exit zero.

12. End-to-end manual validation on the developer machine:
    spawn `foreman daemon start` with a tmp config in the
    background, run `foreman daemon status`, run `foreman daemon
    stop`, verify the output is correct and the daemon exits.

## File-level changes

| File | Change |
|---|---|
| `packages/foreman/src/foreman/cli.py` | Add `_resolve_lock_path` and `_read_lock_file_pid` helpers plus `_STOP_GRACE_SECONDS` / `_STOP_POLL_INTERVAL_SECONDS` module constants. Refactor `daemon_start` to use the helper. Rewrite `daemon_stop` to read PID from lock file, send SIGTERM, poll process liveness for graceful exit. Rewrite `daemon_status` to use the same helpers. **Do not** introduce a separate pid-file lifecycle. |
| `packages/foreman/src/foreman/config.py` | No change. `DaemonConfig.lock_path` already exists from foreman#88; no `pid_path` field added. |
| `packages/foreman/tests/test_cli.py` | Update two pre-existing #88 tests to set `lock_path` (not `pid_path`) in their tmp configs. Add helper unit tests for `_resolve_lock_path` and `_read_lock_file_pid`. Add daemon_stop unit tests (six). Add daemon_status unit tests (two). Add subprocess e2e test that verifies the OS lock is released after `stop`. |

## Alternatives considered

- **Two separate files: pid file + lock file** (the first revision
  of this spec, retired before merge). Rejected: two stale-file
  lifecycles, two failure modes, two places to keep in sync. On CI
  Windows Server 2025 this caused a deterministic mid-pytest hang
  in `test_daemon_start_refuses_second_instance` that did not
  reproduce on local Windows 11 — narrowing the diagnostic surface
  was difficult, and the architectural smell was that we had two
  files doing one logical job. The merged-file design is simpler
  AND avoids the unreproducible-CI bug entirely.

- **Auto-scan for `foreman.*daemon` processes when the lock file
  is missing.** Rejected: cross-platform process scanning needs
  `psutil` (a new dependency) or platform-specific code paths
  (`Get-CimInstance` on Windows, `ps` on POSIX), and the failure
  mode of "kill the wrong process" is worse DX than the clearer
  diagnostic. The `daemon stop` message names the discovery
  command for the operator's platform.

- **Add `psutil` as a dependency for pid liveness checks.**
  Rejected: `os.kill(pid, 0)` is the same primitive `daemon_status`
  has used since #88. Catching `OSError` alongside
  `ProcessLookupError` handles the Windows edge cases.

- **Use `signal.CTRL_BREAK_EVENT` (Windows-specific) for graceful
  shutdown instead of `signal.SIGTERM`.** Out of scope — the
  daemon's signal handler install (`cli.py:386,389`) already swallows
  `NotImplementedError` on Windows, so even with the correct signal,
  graceful shutdown on Windows is broken at a deeper layer. The
  current behaviour (Windows = `TerminateProcess` hard kill, lock
  freed via process death) is correct for the lock-file lifecycle
  even without graceful shutdown.

- **Unlink the lock file from inside `daemon stop`.** Rejected:
  this races concurrent `daemon start`. The OS lock is held by an
  open file descriptor; the file's *existence* isn't the mutex. The
  daemon process dying is the mutex release. Leaving the file in
  place keeps the invariant simple ("lock file content is the most
  recent acquirer's PID") and the next start overwrites it.

- **Make `daemon stop` block on `proc.wait()` instead of polling
  the PID.** `daemon stop` is launched in a separate process from
  the daemon, so it doesn't have a `subprocess.Popen` handle.
  Polling `os.kill(pid, 0)` is the cross-process equivalent.

## Open questions

(none — the bug is reproduced, the fix is structurally tight, and
every acceptance criterion maps to a specific test plus the manual
end-to-end validation step.)

## Out of scope

- **Windows graceful shutdown for the daemon.** `os.kill(pid,
  SIGTERM)` on Windows is `TerminateProcess`; making the daemon
  shut down gracefully on Windows is a separate concern. The
  lock-release-on-process-death contract holds regardless of
  graceful vs hard exit.

- **Daemonizing (`fork + setsid + detach`) on POSIX.** Today
  `daemon start` runs foreground; backgrounding it is a larger UX
  change with implications for log redirection, working directory,
  and tty handling. Out of scope here.

- **Multi-daemon / multi-config support.** The `lock_path` config
  field is already forward-compatible (different configs →
  different lock files). No need to design multi-daemon
  coordination here.

- **foreman#44 orchestrator-bot token refresh.** The bug captured
  on 2026-06-02 was masked by the daemon dying after ~1hr due to
  token expiry (foreman#44), so the "stop doesn't work" issue
  was harder to notice in production. Fixing #44 is separate.

- **Updating `docs/superpowers/specs/2026-06-01-foreman-daemon-design.md`
  to document the lock-file dual role.** The behavior is documented
  by the helper docstrings and the comment block above the
  helpers in `cli.py`; fold into the architectural spec next time
  it's touched.
