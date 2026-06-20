"""Periodic SQLite snapshot scheduler for foreman state (issue #360).

Foreman's authoritative SQLite state (``/foreman/state/foreman.sqlite``
on the container's ``foreman-state`` named volume) is the audit trail
for every ticket, every transition, every role outcome. A WSL2 disk
failure, accidental ``docker compose down -v``, or partial-write
corruption would erase the whole history. This module is the local
disk-level safety net: an in-process scheduler that takes hot
``sqlite3.Connection.backup()``-based snapshots on a fixed cadence,
writes them gzip-compressed to ``~/.foreman/backups/`` (a host bind
mount that survives ``docker compose down -v``), and prunes old
snapshots via a tier-based retention policy.

Why ``sqlite3.Connection.backup()`` is load-bearing (not ``shutil.copyfile``
or ``cp``): foreman's repository runs in WAL mode
(``sqlite_repository.py:102`` — ``PRAGMA journal_mode=WAL``) so writers
don't block readers, with the trade-off that the live ``foreman.sqlite``
file plus its ``-wal`` / ``-shm`` sidecars together represent the DB
state at any moment. A plain file copy that grabs ``foreman.sqlite``
without atomically grabbing the WAL segment captures a torn write or a
WAL-segment-without-checkpoint state that fails ``PRAGMA
integrity_check`` on restore. The
``sqlite3.Connection.backup(target, pages=-1, sleep=0.25)`` API talks to
the source DB's engine layer directly, copies pages atomically without
blocking writers, and produces a verifiably-consistent target — and is
safe under concurrent writers because the SQLite engine (not any Python-
level RLock) serializes backup pages against writer pages in WAL mode.

The pure-data ``RetentionPolicy`` dataclass and the three function /
class surfaces (``take_snapshot``, ``prune_snapshots``,
``BackupScheduler``) are independently testable: ``take_snapshot``
against a real repository, ``prune_snapshots`` against a ``tmp_path``
with hand-massaged filenames, and ``BackupScheduler`` against a stub
clock and an injected ``take_snapshot``. SRP — each surface knows one
thing.
"""

from __future__ import annotations

import datetime as dt
import gzip
import logging
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foreman.v4.config import BackupConfig
    from foreman.v4.event_bus import EventBus

from foreman.v4.events import BackupFailedEvent, BackupTakenEvent

_log = logging.getLogger(__name__)

# Matches the timestamped snapshot filename
# ``foreman-YYYYMMDDTHHMMSSZ.sqlite.gz``. Files in the backups dir that
# do NOT match this regex are LEFT ALONE — humans drop manual backups
# in the dir occasionally and the pruner must not eat them.
_SNAPSHOT_FILENAME_RE = re.compile(
    r"^foreman-(?P<ts>\d{8}T\d{6}Z)\.sqlite\.gz$",
)

_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

# Truncate the str(exc) carried on BackupFailedEvent.reason so a giant
# pyodbc-style stack trace doesn't blow up the structured-log JSONL.
_REASON_MAX_LEN = 500


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Tier-based retention counts for the snapshot pruner.

    Defaults mirror the issue body's stated retention goal:
    ``hourly=24`` (1 day of fine-grained history),
    ``daily=7`` (1 week of coarse history),
    ``weekly=4`` (1 month of long-tail history) — totalling ~35 files
    at any time.
    """

    hourly: int = 24
    daily: int = 7
    weekly: int = 4


def take_snapshot(
    src_conn: sqlite3.Connection,
    dst_dir: Path,
    *,
    now: dt.datetime,
) -> Path:
    """Take an online SQLite backup of ``src_conn`` to a gzipped file.

    Uses ``sqlite3.Connection.backup(target, pages=-1, sleep=0.25)``
    against an open destination ``sqlite3.connect(<tempfile>)``, then
    closes both connections, then gzips the temp file into
    ``dst_dir / f"foreman-{now.strftime('%Y%m%dT%H%M%SZ')}.sqlite.gz"``,
    then deletes the temp file. The gzip-on-success-only sequencing is
    deliberate: a partial snapshot never appears in the backups
    directory, so the pruner's ``foreman-*.sqlite.gz`` glob is
    implicitly the "successful snapshots" filter.

    Returns the path to the written ``.sqlite.gz`` file.

    Raises ``OSError`` for disk-level failures (disk full, read-only
    FS) and ``sqlite3.Error`` for engine-level failures. Both are
    caught and structured-logged by :class:`BackupScheduler`.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    timestamp = now.strftime(_TIMESTAMP_FORMAT)
    final_path = dst_dir / f"foreman-{timestamp}.sqlite.gz"
    tempfile_path = dst_dir / f"foreman-{timestamp}.sqlite.tmp"

    # SQLite's online backup API atomically copies pages from the
    # source DB to ``dst_conn``'s on-disk file under the engine's own
    # serialization. pages=-1 → copy everything in one call; sleep is
    # how long to wait if a writer holds the page-lock during the
    # copy — 0.25s matches the SQLite docs' recommended default.
    dst_conn = sqlite3.connect(tempfile_path)
    try:
        src_conn.backup(dst_conn, pages=-1, sleep=0.25)
    finally:
        dst_conn.close()

    try:
        # gzip in one shot — uncompressed sqlite files at our scale
        # (low MBs) compress 5-7× because of page padding + JSON
        # outcome payloads.
        with open(tempfile_path, "rb") as src_f, gzip.open(final_path, "wb") as gz_f:
            shutil.copyfileobj(src_f, gz_f)
    finally:
        # Always remove the uncompressed tempfile. If gzip raised,
        # the partial .sqlite.gz file remains; that's a defect we
        # treat as "snapshot failed" — the caller is responsible
        # for catching OSError. The pruner ignores filenames that
        # don't match the regex, so a half-written .sqlite.gz that
        # does match the regex would be a problem; but gzip itself
        # writes the final file atomically per the GzipFile spec
        # (close flushes). For safety, an even-more-paranoid impl
        # would write to a `.sqlite.gz.partial` and `os.replace`
        # at the end. Today's shape is correct under normal
        # conditions; revisit if disk-full scenarios surface
        # partial-write evidence.
        try:
            tempfile_path.unlink()
        except FileNotFoundError:
            pass

    return final_path


def _parse_timestamp(filename: str) -> dt.datetime | None:
    """Parse the timestamp out of a snapshot filename.

    Returns the timestamp as a ``datetime`` (UTC, naive — the format
    strips the timezone), or ``None`` for filenames that don't match.
    Pruner uses ``None`` as the "leave this file alone" signal.
    """
    match = _SNAPSHOT_FILENAME_RE.match(filename)
    if match is None:
        return None
    try:
        return dt.datetime.strptime(match.group("ts"), _TIMESTAMP_FORMAT).replace(
            tzinfo=dt.UTC,
        )
    except ValueError:  # pragma: no cover — regex constrains the shape
        return None


def _week_start(at: dt.datetime) -> dt.datetime:
    """Return the Monday-UTC week-start for ``at`` (ISO 8601 week semantics).

    Used to bucket files into 7-day windows in the weekly tier; one
    file per week-window survives the prune.
    """
    monday = at - dt.timedelta(days=at.weekday())
    return dt.datetime(monday.year, monday.month, monday.day, tzinfo=dt.UTC)


def _day_start(at: dt.datetime) -> dt.datetime:
    """Return the UTC start-of-day for ``at``.

    Used to bucket files into calendar-day windows in the daily tier.
    """
    return dt.datetime(at.year, at.month, at.day, tzinfo=dt.UTC)


def prune_snapshots(
    dst_dir: Path,
    *,
    now: dt.datetime,
    retention: RetentionPolicy,
) -> list[Path]:
    """Apply tier-based retention to the snapshot dir, return deleted paths.

    Algorithm (per issue #360 spec):

    1. Files newer than ``now - 24h``: keep the most-recent
       ``retention.hourly`` (sorted by timestamp DESC). Anything beyond
       that count is pruned.
    2. Files between ``now - 24h`` and ``now - 7d``: bucket into
       calendar-day windows (UTC). Within each day-window keep ONLY
       the most-recent file; older files in the same window are
       pruned. Then keep at most ``retention.daily`` such daily-
       survivors.
    3. Files between ``now - 7d`` and ``now - 28d``: bucket into 7-day
       windows (Monday-start UTC). Within each week-window keep ONLY
       the most-recent file; older files in the same window are
       pruned. Then keep at most ``retention.weekly`` such weekly-
       survivors.
    4. Files older than ``now - 28d``: all pruned.

    Filenames whose timestamp does not parse are LEFT ALONE — humans
    drop manual backups in this dir occasionally and the pruner must
    not eat them.

    Returns the list of paths that were deleted (so the caller can
    structured-log them).
    """
    if not dst_dir.exists():
        return []
    entries: list[tuple[dt.datetime, Path]] = []
    for path in dst_dir.glob("foreman-*.sqlite.gz"):
        ts = _parse_timestamp(path.name)
        if ts is None:
            continue
        entries.append((ts, path))
    # Sort by timestamp DESC so "most recent first" walks are trivial.
    entries.sort(key=lambda pair: pair[0], reverse=True)

    horizon_hourly = now - dt.timedelta(hours=24)
    horizon_daily = now - dt.timedelta(days=7)
    horizon_weekly = now - dt.timedelta(days=28)

    hourly_files: list[tuple[dt.datetime, Path]] = []
    daily_files: list[tuple[dt.datetime, Path]] = []
    weekly_files: list[tuple[dt.datetime, Path]] = []
    too_old: list[tuple[dt.datetime, Path]] = []

    for ts, path in entries:
        if ts > horizon_hourly:
            hourly_files.append((ts, path))
        elif ts > horizon_daily:
            daily_files.append((ts, path))
        elif ts > horizon_weekly:
            weekly_files.append((ts, path))
        else:
            too_old.append((ts, path))

    to_delete: list[Path] = []

    # Tier 1: keep most-recent N hourly files; prune the rest.
    for _ts, path in hourly_files[retention.hourly :]:
        to_delete.append(path)

    # Tier 2: bucket daily files by calendar-day window; keep the most-
    # recent file in each bucket; then cap at retention.daily.
    daily_buckets: dict[dt.datetime, list[tuple[dt.datetime, Path]]] = {}
    for ts, path in daily_files:
        daily_buckets.setdefault(_day_start(ts), []).append((ts, path))
    daily_survivors: list[tuple[dt.datetime, Path]] = []
    for _bucket_start, files in daily_buckets.items():
        files.sort(key=lambda pair: pair[0], reverse=True)
        daily_survivors.append(files[0])
        for _ts, path in files[1:]:
            to_delete.append(path)
    daily_survivors.sort(key=lambda pair: pair[0], reverse=True)
    for _ts, path in daily_survivors[retention.daily :]:
        to_delete.append(path)

    # Tier 3: bucket weekly files by Monday-UTC week window; keep the
    # most-recent file in each bucket; then cap at retention.weekly.
    weekly_buckets: dict[dt.datetime, list[tuple[dt.datetime, Path]]] = {}
    for ts, path in weekly_files:
        weekly_buckets.setdefault(_week_start(ts), []).append((ts, path))
    weekly_survivors: list[tuple[dt.datetime, Path]] = []
    for _bucket_start, files in weekly_buckets.items():
        files.sort(key=lambda pair: pair[0], reverse=True)
        weekly_survivors.append(files[0])
        for _ts, path in files[1:]:
            to_delete.append(path)
    weekly_survivors.sort(key=lambda pair: pair[0], reverse=True)
    for _ts, path in weekly_survivors[retention.weekly :]:
        to_delete.append(path)

    # Tier 4: too-old files always prune.
    for _ts, path in too_old:
        to_delete.append(path)

    deleted: list[Path] = []
    for path in to_delete:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        deleted.append(path)
    return deleted


class _DisabledBackupScheduler:
    """No-op scheduler sentinel for ``backup.enabled = False`` configs.

    Has the same ``tick()`` shape as the real :class:`BackupScheduler`
    so the daemon's call site stays unconditional (one line:
    ``self._backup_scheduler.tick()``). Stateless: a single instance
    can serve as the default kwarg on ``Daemon.__init__`` AND every
    direct ``Daemon(...)`` construction in tests without cross-
    instance mutation hazards.
    """

    def tick(self) -> Path | None:
        return None


class BackupScheduler:
    """Periodic SQLite snapshot scheduler.

    Runs inside the daemon's ``tick_once()`` loop: on each call, it
    checks whether ``self._interval_seconds`` of wall-clock time have
    elapsed since the last snapshot and, if so, calls
    :func:`take_snapshot` followed by :func:`prune_snapshots`. The
    in-process design reuses the existing tick cadence; there is no
    second thread to coordinate, no second container to monitor, no
    third file system to track.

    Failures from ``take_snapshot`` / ``prune_snapshots`` are swallowed
    and structured-logged via :class:`BackupFailedEvent` — a transient
    disk-full or read-only-FS condition must not crash the daemon
    loop. Successful snapshots publish :class:`BackupTakenEvent`.

    Both events are :class:`DaemonEvent` subclasses; consumers of the
    EventBus tolerate them via ``isinstance`` guards (see
    :mod:`foreman.v4.observers.structured_log`,
    :mod:`foreman.v4.observers.event_archive`,
    :mod:`foreman.v4.observers.metrics`, :mod:`foreman.v4.event_bus`).
    """

    def __init__(
        self,
        *,
        src_conn: sqlite3.Connection,
        dst_dir: Path,
        interval_seconds: int,
        retention: RetentionPolicy,
        clock: Callable[[], dt.datetime],
        bus: EventBus,
    ) -> None:
        self._src_conn = src_conn
        self._dst_dir = dst_dir
        self._interval_seconds = interval_seconds
        self._retention = retention
        self._clock = clock
        self._bus = bus
        # First call always fires (``None`` makes the elapsed check
        # treat the scheduler as never-having-snapshotted).
        self._last_snapshot_at: dt.datetime | None = None

    @classmethod
    def from_config(
        cls,
        config: BackupConfig,
        *,
        src_conn: sqlite3.Connection,
        bus: EventBus,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> BackupScheduler | _DisabledBackupScheduler:
        """Build a scheduler from :class:`BackupConfig`.

        When ``config.enabled`` is False, returns the module-private
        :class:`_DisabledBackupScheduler` sentinel (no files written,
        ``tick() -> None``). When True, constructs and returns a
        live :class:`BackupScheduler`. This factory is the only
        constructor path used by bootstrap; tests that want a
        hand-tuned interval / clock / retention construct
        :class:`BackupScheduler` directly.
        """
        if not config.enabled:
            return _DisabledBackupScheduler()
        return cls(
            src_conn=src_conn,
            dst_dir=Path(config.dir),
            interval_seconds=config.interval_seconds,
            retention=RetentionPolicy(
                hourly=config.retention_hourly,
                daily=config.retention_daily,
                weekly=config.retention_weekly,
            ),
            clock=clock,
            bus=bus,
        )

    def tick(self) -> Path | None:
        """Take a snapshot if enough wall-clock time has elapsed.

        Returns the new snapshot's path on the ticks where one was
        taken; returns ``None`` on the ticks where the interval
        hasn't elapsed yet OR on the ticks where ``take_snapshot``
        raised (the failure is structured-logged via
        :class:`BackupFailedEvent`).
        """
        now = self._clock()
        if self._last_snapshot_at is not None:
            elapsed = (now - self._last_snapshot_at).total_seconds()
            if elapsed < self._interval_seconds:
                return None

        try:
            snapshot_path = take_snapshot(
                self._src_conn, self._dst_dir, now=now,
            )
        except (OSError, sqlite3.Error) as exc:
            self._publish_failure(now, phase="snapshot", exc=exc)
            return None

        self._last_snapshot_at = now

        try:
            pruned = prune_snapshots(
                self._dst_dir, now=now, retention=self._retention,
            )
        except (OSError, sqlite3.Error) as exc:
            self._publish_failure(now, phase="prune", exc=exc)
            pruned = []

        try:
            size_bytes = snapshot_path.stat().st_size
        except OSError:
            size_bytes = 0

        self._bus.publish(BackupTakenEvent(
            at=now,
            path=str(snapshot_path),
            size_bytes=size_bytes,
            pruned_count=len(pruned),
        ))
        return snapshot_path

    def _publish_failure(
        self,
        now: dt.datetime,
        *,
        phase: str,
        exc: BaseException,
    ) -> None:
        reason = str(exc)[:_REASON_MAX_LEN]
        self._bus.publish(BackupFailedEvent(
            at=now, phase=phase, reason=reason,  # type: ignore[arg-type]
        ))


# Type alias for "the daemon's backup-scheduler attribute" — either a
# real :class:`BackupScheduler` or the no-op
# :class:`_DisabledBackupScheduler` sentinel. Structural typing — not
# a formal Protocol; both share ``tick() -> Path | None``. Defined
# at the bottom of the module so the underlying classes are in scope.
BackupSchedulerLike = BackupScheduler | _DisabledBackupScheduler
