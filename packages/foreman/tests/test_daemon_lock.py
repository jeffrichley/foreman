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
    # File remains after release; the OS lock state, not the file's
    # existence, is the mutex.
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
    # Wait for the holder to print "locked" so we know it has the
    # lock AND has written its PID to the file.
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
        # The PID written to the file is the inner-Python PID, which
        # may differ from ``holder.pid`` (the Popen child) on Windows
        # when ``sys.executable`` is a launcher that re-execs into a
        # different process. The file content is what the error
        # message should name — that's the value any operator looking
        # at the lock file would read.
        holder_pid_from_file = lock_path.read_text(encoding="ascii").strip()
        assert holder_pid_from_file.isdigit(), (
            f"holder did not write a numeric PID: {holder_pid_from_file!r}"
        )
        with pytest.raises(LockAcquisitionError) as excinfo:
            with DaemonLock(lock_path):
                pass
        assert "already running" in str(excinfo.value)
        assert holder_pid_from_file in str(excinfo.value)
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
