"""OS-level exclusive lock for the foreman daemon's start-up mutex.

Used by ``foreman daemon start`` to refuse a second concurrent launch
(foreman#88). The lock auto-releases on process death, so no stale-
cleanup logic is needed: a crashed daemon's lock is freed the moment
the kernel reaps the process.

Coordinates with foreman#72's pid-file: this lock is the primary
duplicate-detection mutex; the pid file is the operator-facing handle
for ``foreman daemon stop``. Both files coexist when both specs are
implemented; they have different purposes and different lifecycles.
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

    On enter: opens ``path`` (creating it and parent dirs if needed),
    acquires an exclusive non-blocking OS lock, and writes
    ``str(os.getpid())`` to the file's contents.

    On exit (or process death): the OS releases the lock.

    On lock-already-held: raises ``LockAcquisitionError`` with a
    message naming the holder's PID (read from the file's contents,
    or "unknown" if the file is unreadable).
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path).expanduser()
        self._fd: int | None = None

    def __enter__(self) -> DaemonLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT so the file appears on first run; O_RDWR so we can
        # both write our PID into it AND read the holder's PID from
        # it on a failed acquisition (read does not need the lock).
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
            # Closing the fd releases the OS lock. We intentionally do
            # NOT delete the file — its existence isn't the mutex; the
            # OS lock is.
            os.close(self._fd)
            self._fd = None


_WINDOWS_LOCK_OFFSET = 1024


def _acquire_exclusive_nonblocking(fd: int) -> None:
    """Try-once exclusive lock on ``fd``. Raises on contention."""
    if sys.platform == "win32":
        import msvcrt

        # Lock 1 byte at offset 1024 — well past where we write the
        # PID (which fits in <20 bytes). Windows mandatory locks block
        # reads of locked bytes; locking a high offset keeps the PID
        # at byte 0 readable both by the same process (so the body
        # of the ``with DaemonLock(...)`` block can verify content)
        # and by other processes attempting acquisition (so they can
        # name the holder PID in their error message).
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        finally:
            # Restore the file pointer to 0 so subsequent writes land
            # at the start of the file (where the PID belongs).
            os.lseek(fd, 0, os.SEEK_SET)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _read_holder_pid(path: Path) -> int | None:
    """Best-effort: parse the PID written by the lock holder.

    Returns ``None`` if the file is unreadable or its content doesn't
    parse as an int (transient: holder wrote nothing yet, or wrote
    garbage).
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
    # Canonical form: "(pid: <token>)" for both known PID and the
    # unknown fallback (issue #88 acceptance criterion).
    pid_part = str(pid) if pid is not None else "unknown"
    return (
        f"Foreman daemon is already running (pid: {pid_part}). "
        f"Use `foreman daemon stop` to stop it first."
    )
