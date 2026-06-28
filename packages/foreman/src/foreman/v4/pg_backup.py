"""Postgres pg_dump backup scheduler (foreman#434).

Provides:
  - ``RetentionPolicy``       — frozen dataclass: hourly/daily/weekly counts.
  - ``take_snapshot``         — shell out to pg_dump, gzip-compress output.
  - ``prune_snapshots``       — apply three-tier retention algorithm.
  - ``BackupScheduler``       — tick-driven coordinator; publishes events.
  - ``_DisabledBackupScheduler`` — no-op sentinel (enabled=False path).
  - ``BackupSchedulerLike``   — type alias for the union.
  - ``BackupScheduler.from_config`` — classmethod factory from BackupConfig.

SRP split (Decision 4): ``take_snapshot`` knows only how to shell out to
pg_dump and gzip the output. ``prune_snapshots`` knows only the three-tier
retention algorithm. ``BackupScheduler`` knows only when to call the other
two. ``Daemon.tick_once()`` knows nothing about what a snapshot is.
"""

from __future__ import annotations

import datetime as dt
import gzip
import os
import re
import subprocess
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foreman.v4.config import BackupConfig
    from foreman.v4.event_bus import EventBus

# Pattern for foreman snapshot filenames: foreman-YYYYMMDDTHHMMSSZ.sql.gz
_SNAPSHOT_FILENAME_RE = re.compile(
    r"^foreman-(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z\.sql\.gz$"
)

# Subprocess timeout (seconds) for pg_dump — prevents a hung sidecar from
# blocking the daemon main loop indefinitely (foreman#309 lesson).
_PG_DUMP_TIMEOUT = 300


def _dsn_without_password(dsn: str) -> str:
    """Return the DSN with the password stripped, safe for argv exposure.

    Avoids leaking credentials via ``ps``/``/proc/<pid>/cmdline`` for the
    lifetime of a ``pg_dump`` or ``psql`` process.  If the DSN carries no
    password the original string is returned unchanged.
    """
    parsed = urllib.parse.urlparse(dsn)
    if not parsed.password:
        return dsn
    userinfo = parsed.username or ""
    host = parsed.hostname or ""
    port_suffix = f":{parsed.port}" if parsed.port else ""
    netloc = f"{userinfo}@{host}{port_suffix}" if userinfo else f"{host}{port_suffix}"
    return urllib.parse.urlunparse((
        parsed.scheme,
        netloc,
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))


def _subprocess_pg_env(dsn: str) -> dict[str, str]:
    """Return ``os.environ`` copy with ``PGPASSWORD`` set from the DSN.

    Used alongside :func:`_dsn_without_password` so the password never
    appears on the command line while still being available to
    ``pg_dump``/``psql`` via the libpq environment variable.
    """
    parsed = urllib.parse.urlparse(dsn)
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    return env


@dataclass(frozen=True)
class RetentionPolicy:
    """Three-tier retention counts for the prune algorithm.

    ``hourly`` — keep this many most-recent files in the last 24h window.
    ``daily``  — keep this many most-recent calendar-day survivors in the
                 ``[now-7d, now-24h)`` window.
    ``weekly`` — keep this many most-recent ISO-week survivors in the
                 ``[now-28d, now-7d)`` window.
    """

    hourly: int = 24
    daily: int = 7
    weekly: int = 4


def take_snapshot(dsn: str, dst_dir: Path, *, now: dt.datetime) -> Path:
    """Run pg_dump and gzip-compress the output to dst_dir.

    Creates ``dst_dir`` (with parents) if it doesn't already exist.
    Returns the path of the written ``.sql.gz`` file.

    The DSN password is passed via ``PGPASSWORD`` (never on argv).
    A hard timeout of :data:`_PG_DUMP_TIMEOUT` seconds guards the daemon
    main loop — ``subprocess.TimeoutExpired`` (a ``SubprocessError``)
    propagates to the caller and is caught by ``BackupScheduler.tick``.

    Raises ``subprocess.CalledProcessError`` on non-zero pg_dump exit.
    Raises ``subprocess.TimeoutExpired`` when pg_dump hangs.
    Raises ``OSError`` on I/O failures. Does NOT swallow — callers
    (``BackupScheduler.tick``) handle exceptions at the call site.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)

    safe_dsn = _dsn_without_password(dsn)
    env = _subprocess_pg_env(dsn)

    result = subprocess.run(
        [
            "pg_dump",
            "--format=plain",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-acl",
            safe_dsn,
        ],
        capture_output=True,
        check=True,
        timeout=_PG_DUMP_TIMEOUT,
        env=env,
    )

    filename = f"foreman-{now.strftime('%Y%m%dT%H%M%SZ')}.sql.gz"
    out_path = dst_dir / filename
    out_path.write_bytes(gzip.compress(result.stdout))
    return out_path


def _parse_snapshot_timestamp(path: Path) -> dt.datetime | None:
    """Parse the UTC timestamp encoded in a snapshot filename.

    Returns ``None`` for filenames that don't match the expected pattern
    (e.g. manual backups or unrelated files dropped in the directory).
    """
    m = _SNAPSHOT_FILENAME_RE.match(path.name)
    if m is None:
        return None
    year, month, day, hour, minute, second = (int(x) for x in m.groups())
    try:
        return dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.UTC)
    except ValueError:
        return None


def prune_snapshots(
    dst_dir: Path,
    *,
    now: dt.datetime,
    retention: RetentionPolicy,
) -> list[Path]:
    """Apply three-tier retention algorithm to ``dst_dir``.

    Globs ``foreman-*.sql.gz``, parses timestamps, applies tiers:

    1. Files in ``[now, now-24h)``: keep most-recent ``retention.hourly``; prune rest.
    2. Files in ``[now-7d, now-24h)``: bucket by UTC calendar day; keep
       most-recent per day; then keep at most ``retention.daily`` day-survivors
       (most-recent days win); prune rest.
    3. Files in ``[now-28d, now-7d)``: bucket by ISO 8601 week (Monday UTC);
       keep most-recent per week; then keep at most ``retention.weekly``
       week-survivors; prune rest.
    4. Files older than ``now-28d``: all pruned.

    Files with unparseable names are left alone (operators may drop manual
    backups in this directory).

    Returns the list of deleted :class:`Path` objects.
    """
    horizon_hourly = now - dt.timedelta(hours=24)
    horizon_daily = now - dt.timedelta(days=7)
    horizon_weekly = now - dt.timedelta(days=28)

    # Collect all parseable snapshots with their timestamps.
    snapshots: list[tuple[dt.datetime, Path]] = []
    for p in dst_dir.glob("foreman-*.sql.gz"):
        ts = _parse_snapshot_timestamp(p)
        if ts is None:
            continue  # leave unparseable filenames alone
        snapshots.append((ts, p))

    # Sort by timestamp descending (most-recent first) for tier processing.
    snapshots.sort(key=lambda x: x[0], reverse=True)

    to_keep: set[Path] = set()

    # ------------------------------------------------------------------
    # Tier 1: hourly window — [now-24h, now]
    # ------------------------------------------------------------------
    hourly = [(ts, p) for ts, p in snapshots if ts >= horizon_hourly]
    # Most-recent ``retention.hourly`` survive.
    for _, p in hourly[: retention.hourly]:
        to_keep.add(p)
    # The rest beyond the limit are NOT added to to_keep → will be pruned.

    # ------------------------------------------------------------------
    # Tier 2: daily window — [now-7d, now-24h)
    # ------------------------------------------------------------------
    daily_window = [
        (ts, p) for ts, p in snapshots
        if horizon_daily <= ts < horizon_hourly
    ]
    # Bucket by UTC calendar date (YYYY-MM-DD); keep most-recent per day.
    day_survivors: dict[str, tuple[dt.datetime, Path]] = {}
    for ts, p in daily_window:
        day_key = ts.strftime("%Y-%m-%d")
        if day_key not in day_survivors or ts > day_survivors[day_key][0]:
            day_survivors[day_key] = (ts, p)
    # Sort day survivors by their representative timestamp descending.
    sorted_day_survivors = sorted(
        day_survivors.values(), key=lambda x: x[0], reverse=True
    )
    # Keep most-recent ``retention.daily`` days.
    for _, p in sorted_day_survivors[: retention.daily]:
        to_keep.add(p)

    # ------------------------------------------------------------------
    # Tier 3: weekly window — [now-28d, now-7d)
    # ------------------------------------------------------------------
    weekly_window = [
        (ts, p) for ts, p in snapshots
        if horizon_weekly <= ts < horizon_daily
    ]
    # Bucket by ISO 8601 week (Monday UTC); keep most-recent per week.
    week_survivors: dict[tuple[int, int], tuple[dt.datetime, Path]] = {}
    for ts, p in weekly_window:
        week_key = ts.isocalendar()[:2]  # (ISO year, ISO week)
        if week_key not in week_survivors or ts > week_survivors[week_key][0]:
            week_survivors[week_key] = (ts, p)
    sorted_week_survivors = sorted(
        week_survivors.values(), key=lambda x: x[0], reverse=True
    )
    for _, p in sorted_week_survivors[: retention.weekly]:
        to_keep.add(p)

    # ------------------------------------------------------------------
    # Prune: delete all parseable snapshots NOT in to_keep.
    # ------------------------------------------------------------------
    deleted: list[Path] = []
    for _, p in snapshots:
        if p not in to_keep:
            p.unlink(missing_ok=True)
            deleted.append(p)

    return deleted


class _DisabledBackupScheduler:
    """No-op sentinel for the ``enabled=False`` path.

    Stateless — safe to use as a module-level default kwarg so tests
    that don't pass ``backup_scheduler=`` stay green without change.
    ``tick()`` returns ``None`` immediately and writes nothing.
    """

    def tick(self) -> Path | None:
        return None


class BackupScheduler:
    """Tick-driven pg_dump backup coordinator.

    Constructed via :meth:`from_config` in production and directly in
    tests. Publishes :class:`~foreman.v4.events.BackupTakenEvent` on
    success or :class:`~foreman.v4.events.BackupFailedEvent` on failure
    (either phase); both swallowed so one bad snapshot can't crash the
    daemon loop.

    Clock is injectable (defaults to ``dt.datetime.now(dt.UTC)``) so
    tests can control the wall-clock without ``time.sleep``.
    """

    def __init__(
        self,
        *,
        dsn: str,
        dst_dir: Path,
        interval_seconds: int,
        retention: RetentionPolicy,
        clock: Callable[[], dt.datetime],
        bus: EventBus,
    ) -> None:
        self._dsn = dsn
        self._dst_dir = dst_dir
        self._interval_seconds = interval_seconds
        self._retention = retention
        self._clock = clock
        self._bus = bus
        self._last_snapshot_at: dt.datetime | None = None

    def tick(self) -> Path | None:
        """Maybe take a snapshot.

        Reads the clock once. Skips if not enough wall-clock time has
        elapsed since the last snapshot (except on first call, which
        always fires). Returns the snapshot path on success or ``None``
        on skip or failure.
        """
        from foreman.v4.events import BackupFailedEvent, BackupTakenEvent

        now = self._clock()

        if self._last_snapshot_at is not None:
            elapsed = (now - self._last_snapshot_at).total_seconds()
            if elapsed < self._interval_seconds:
                return None

        # --- take snapshot ---
        snap_path: Path | None = None
        try:
            snap_path = take_snapshot(dsn=self._dsn, dst_dir=self._dst_dir, now=now)
        except (OSError, subprocess.SubprocessError) as exc:
            self._bus.publish(
                BackupFailedEvent(
                    at=now,
                    phase="snapshot",
                    reason=str(exc)[:500],
                )
            )
            return None

        self._last_snapshot_at = now

        # --- prune old snapshots ---
        pruned: list[Path] = []
        try:
            pruned = prune_snapshots(
                dst_dir=self._dst_dir, now=now, retention=self._retention
            )
        except OSError as exc:
            self._bus.publish(
                BackupFailedEvent(
                    at=now,
                    phase="prune",
                    reason=str(exc)[:500],
                )
            )
            # Pruning failure does NOT cancel the snapshot; still return snap_path.
            self._bus.publish(
                BackupTakenEvent(
                    at=now,
                    path=str(snap_path),
                    size_bytes=snap_path.stat().st_size,
                    pruned_count=0,
                )
            )
            return snap_path

        self._bus.publish(
            BackupTakenEvent(
                at=now,
                path=str(snap_path),
                size_bytes=snap_path.stat().st_size,
                pruned_count=len(pruned),
            )
        )
        return snap_path

    @classmethod
    def from_config(
        cls,
        config: BackupConfig,
        *,
        dsn: str,
        bus: EventBus,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> BackupScheduler | _DisabledBackupScheduler:
        """Factory: return a real scheduler or the disabled sentinel.

        Returns ``_DisabledBackupScheduler()`` when ``config.enabled``
        is ``False``; otherwise constructs and returns a real
        ``BackupScheduler`` from the config fields.
        """
        if not config.enabled:
            return _DisabledBackupScheduler()
        return cls(
            dsn=dsn,
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


# Type alias for consumers (Daemon, bootstrap) that accept either flavor.
# Defined after both classes so it's a real union (not a string forward ref).
BackupSchedulerLike = BackupScheduler | _DisabledBackupScheduler
