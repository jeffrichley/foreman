"""Tests for the pg_dump backup scheduler (foreman#434).

Covers: take_snapshot, prune_snapshots, BackupScheduler, _DisabledBackupScheduler,
BackupScheduler.from_config.
"""

from __future__ import annotations

import datetime as dt
import gzip
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from foreman.v4.config import BackupConfig
from foreman.v4.event_bus import EventBus
from foreman.v4.events import BackupFailedEvent
from foreman.v4.pg_backup import (
    BackupScheduler,
    RetentionPolicy,
    _DisabledBackupScheduler,
    prune_snapshots,
    take_snapshot,
)

# Fixed datetime for deterministic filename assertions.
_FIXED_DT = dt.datetime(2026, 6, 27, 12, 0, 0, tzinfo=dt.UTC)
_FIXED_DSN = "postgresql://foreman:pw@localhost:5432/foreman"


# ---------------------------------------------------------------------------
# take_snapshot
# ---------------------------------------------------------------------------


def test_take_snapshot_writes_gz_and_returns_path(tmp_path: Path) -> None:
    """Happy path: pg_dump produces output, snapshot is written as .sql.gz."""
    dump_bytes = b"-- PostgreSQL database dump\nCREATE TABLE tickets ();\n"
    mock_result = subprocess.CompletedProcess(
        args=["pg_dump"],
        returncode=0,
        stdout=dump_bytes,
        stderr=b"",
    )
    with patch("subprocess.run", return_value=mock_result):
        result_path = take_snapshot(
            dsn=_FIXED_DSN,
            dst_dir=tmp_path,
            now=_FIXED_DT,
        )

    assert result_path.exists()
    assert result_path.parent == tmp_path
    # Filename must match the timestamp-encoded pattern
    import re

    assert re.match(r"^foreman-\d{8}T\d{6}Z\.sql\.gz$", result_path.name)
    # Decompressing must yield the original dump bytes
    assert gzip.decompress(result_path.read_bytes()) == dump_bytes


def test_take_snapshot_propagates_pg_dump_failure(tmp_path: Path) -> None:
    """A non-zero pg_dump exit raises CalledProcessError; no file written."""
    with patch(
        "subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "pg_dump"),
    ):
        with pytest.raises(subprocess.CalledProcessError):
            take_snapshot(dsn=_FIXED_DSN, dst_dir=tmp_path, now=_FIXED_DT)

    # No .sql.gz file should have been created.
    assert list(tmp_path.glob("*.sql.gz")) == []


def test_take_snapshot_creates_dst_dir_if_missing(tmp_path: Path) -> None:
    """dst_dir is created (parents=True) if it doesn't already exist."""
    nested_dir = tmp_path / "new" / "sub"
    assert not nested_dir.exists()

    dump_bytes = b"-- test"
    mock_result = subprocess.CompletedProcess(
        args=["pg_dump"], returncode=0, stdout=dump_bytes, stderr=b""
    )
    with patch("subprocess.run", return_value=mock_result):
        result_path = take_snapshot(
            dsn=_FIXED_DSN,
            dst_dir=nested_dir,
            now=_FIXED_DT,
        )

    assert nested_dir.exists()
    assert result_path.exists()


# ---------------------------------------------------------------------------
# prune_snapshots
# ---------------------------------------------------------------------------


def _make_snapshot(dst_dir: Path, ts: dt.datetime) -> Path:
    """Write a dummy .sql.gz file with a timestamp-encoded name."""
    filename = f"foreman-{ts.strftime('%Y%m%dT%H%M%SZ')}.sql.gz"
    path = dst_dir / filename
    path.write_bytes(gzip.compress(b"-- dummy"))
    # Adjust mtime to match the timestamp (for OS-level ordering sanity).
    ts_unix = ts.timestamp()
    os.utime(path, (ts_unix, ts_unix))
    return path


def test_prune_keeps_hourly_then_daily_then_weekly(tmp_path: Path) -> None:
    """Seed with 60 hourly + 30 daily files; assert exactly 35 survivors.

    With now = 2026-06-27 12:00:00 UTC and RetentionPolicy(24, 7, 4):
    - Hourly window [now-24h, now]: 25 unique files; keep 24 most-recent.
    - Daily window [now-7d, now-24h): 7 distinct calendar days (Jun 20–26);
      keep most-recent per day → 7 survivors.
    - Weekly window [now-28d, now-7d): 21 daily-spaced files spanning ISO
      weeks 22–25 (2026); keep most-recent per week → 4 survivors.
    Total: 24 + 7 + 4 = 35.
    """
    now = dt.datetime(2026, 6, 27, 12, 0, 0, tzinfo=dt.UTC)

    # Seed 1: 60 hourly files (now-59h through now).
    for i in range(60):
        ts = now - dt.timedelta(hours=60 - i - 1)
        _make_snapshot(tmp_path, ts)

    # Seed 2: 30 daily files (now-30d through now-1d).
    # Files at now-30d and now-29d fall before all retention windows and
    # are pruned unconditionally. Files at now-1d and now-2d share
    # timestamps with hourly files (no-op collision: same filename, same bytes).
    daily_ts: list[dt.datetime] = []
    for i in range(30):
        ts = now - dt.timedelta(days=30 - i)
        _make_snapshot(tmp_path, ts)
        daily_ts.append(ts)

    retention = RetentionPolicy(hourly=24, daily=7, weekly=4)
    pruned = prune_snapshots(dst_dir=tmp_path, now=now, retention=retention)
    survivors = list(tmp_path.glob("foreman-*.sql.gz"))

    # No pruned file may remain on disk.
    for p in pruned:
        assert not p.exists(), f"Pruned file still exists: {p}"

    # Exact total must be 24 (hourly) + 7 (daily) + 4 (weekly) = 35.
    assert len(survivors) == 35

    for p in survivors:
        assert p.exists()

    # Verify weekly-tier: the most-recent file per ISO week must survive.
    # Daily files i=2..22 (now-28d..now-8d) fall in the weekly window,
    # spanning 4 ISO weeks (22–25 of 2026). The most-recent per week is:
    #   week 22 → now-27d (2026-05-31), week 23 → now-20d (2026-06-07),
    #   week 24 → now-13d (2026-06-14), week 25 → now-8d  (2026-06-19).
    horizon_weekly = now - dt.timedelta(days=28)
    horizon_daily = now - dt.timedelta(days=7)

    week_best: dict[tuple[int, int], dt.datetime] = {}
    for ts in daily_ts:
        if horizon_weekly <= ts < horizon_daily:
            wk = (ts.isocalendar()[0], ts.isocalendar()[1])
            if wk not in week_best or ts > week_best[wk]:
                week_best[wk] = ts

    assert len(week_best) == 4, f"Expected 4 ISO weeks in weekly window, got {len(week_best)}"
    survivor_names = {p.name for p in survivors}
    for wk, best_ts in week_best.items():
        name = f"foreman-{best_ts.strftime('%Y%m%dT%H%M%SZ')}.sql.gz"
        assert name in survivor_names, f"Most-recent file for ISO week {wk} ({name}) was pruned"


def test_prune_leaves_unparseable_filenames_alone(tmp_path: Path) -> None:
    """Files that don't match the foreman-<ts>.sql.gz pattern are untouched."""
    # One valid snapshot
    valid = _make_snapshot(tmp_path, _FIXED_DT - dt.timedelta(days=60))
    # One manual backup with a non-matching name
    manual = tmp_path / "manual-backup.sql.gz"
    manual.write_bytes(gzip.compress(b"manual"))

    now = _FIXED_DT
    pruned = prune_snapshots(
        dst_dir=tmp_path,
        now=now,
        retention=RetentionPolicy(hourly=24, daily=7, weekly=4),
    )

    # The unparseable file must not be touched.
    assert manual.exists(), "manual-backup.sql.gz must survive pruning"
    # The valid snapshot older than 28d IS pruned (out of all retention windows).
    assert valid in pruned


# ---------------------------------------------------------------------------
# BackupScheduler
# ---------------------------------------------------------------------------


def test_scheduler_tick_respects_interval(tmp_path: Path) -> None:
    """First tick fires; tick within interval skips; tick past interval fires again."""
    clock_time: list[dt.datetime] = [dt.datetime(2026, 6, 27, 10, 0, 0, tzinfo=dt.UTC)]

    def clock() -> dt.datetime:
        return clock_time[0]

    bus = EventBus()
    dump_bytes = b"-- test"
    mock_result = subprocess.CompletedProcess(
        args=["pg_dump"], returncode=0, stdout=dump_bytes, stderr=b""
    )

    scheduler = BackupScheduler(
        dsn=_FIXED_DSN,
        dst_dir=tmp_path,
        interval_seconds=3600,
        retention=RetentionPolicy(),
        clock=clock,
        bus=bus,
    )

    with patch("subprocess.run", return_value=mock_result):
        # First tick: always fires (no last snapshot).
        result1 = scheduler.tick()
    assert result1 is not None, "First tick must fire a snapshot"

    with patch("subprocess.run", return_value=mock_result):
        # Advance 30 min — too soon.
        clock_time[0] = dt.datetime(2026, 6, 27, 10, 30, 0, tzinfo=dt.UTC)
        result2 = scheduler.tick()
    assert result2 is None, "Tick within interval must skip"

    with patch("subprocess.run", return_value=mock_result):
        # Advance 31 more minutes (total 61 min from first tick).
        clock_time[0] = dt.datetime(2026, 6, 27, 11, 1, 0, tzinfo=dt.UTC)
        result3 = scheduler.tick()
    assert result3 is not None, "Tick past interval must fire a snapshot"


def test_scheduler_disabled_via_from_config_returns_none(tmp_path: Path) -> None:
    """BackupScheduler.from_config with enabled=False returns _DisabledBackupScheduler."""
    config = BackupConfig(enabled=False)
    bus = EventBus()
    scheduler = BackupScheduler.from_config(config, dsn=_FIXED_DSN, bus=bus)
    assert isinstance(scheduler, _DisabledBackupScheduler)
    assert scheduler.tick() is None


def test_scheduler_swallows_snapshot_error_and_publishes_failed_event(
    tmp_path: Path,
) -> None:
    """OSError during take_snapshot is swallowed; BackupFailedEvent published."""
    captured_events: list = []

    def capture(event: object) -> None:
        captured_events.append(event)

    clock_time = dt.datetime(2026, 6, 27, 12, 0, 0, tzinfo=dt.UTC)
    bus = EventBus()
    bus.subscribe(capture)

    scheduler = BackupScheduler(
        dsn=_FIXED_DSN,
        dst_dir=tmp_path,
        interval_seconds=3600,
        retention=RetentionPolicy(),
        clock=lambda: clock_time,
        bus=bus,
    )

    with patch(
        "foreman.v4.pg_backup.take_snapshot",
        side_effect=OSError("disk full"),
    ):
        result = scheduler.tick()

    assert result is None, "Snapshot failure must return None"
    failed_events = [e for e in captured_events if isinstance(e, BackupFailedEvent)]
    assert len(failed_events) == 1
    assert failed_events[0].phase == "snapshot"
    assert "disk full" in failed_events[0].reason
