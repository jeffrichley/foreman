"""state_backup — periodic SQLite snapshot scheduler (issue #360).

Covers:
- ``take_snapshot`` produces a gzipped file that decompresses to an
  integrity-check-clean SQLite with the same row counts as the live DB.
- Filename matches the sortable ISO regex so name-order = time-order.
- ``prune_snapshots`` tier algorithm keeps the expected per-tier counts
  AND leaves un-parseable filenames alone.
- ``BackupScheduler.tick`` respects the configured interval.
- ``BackupScheduler.from_config(enabled=False)`` returns the
  ``_DisabledBackupScheduler`` sentinel and ``tick()`` is a no-op.
- The scheduler swallows ``OSError`` from ``take_snapshot`` and
  publishes a ``BackupFailedEvent`` on the injected bus.
"""

from __future__ import annotations

import datetime as dt
import gzip
import sqlite3
from pathlib import Path

import pytest

from foreman.v4.config import BackupConfig
from foreman.v4.event_bus import EventBus
from foreman.v4.events import (
    BackupFailedEvent,
    BackupTakenEvent,
)
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.state_backup import (
    BackupScheduler,
    RetentionPolicy,
    _DisabledBackupScheduler,
    prune_snapshots,
    take_snapshot,
)

_T0 = dt.datetime(2026, 6, 13, 12, 0, 0, tzinfo=dt.UTC)


def _seed_live_db(tmp_path: Path) -> tuple[SqliteTicketRepository, Path]:
    """Build a SqliteTicketRepository with a few tickets + state-instances."""
    db_path = tmp_path / "live.db"
    repo = SqliteTicketRepository.at_path(db_path)
    for n in range(3):
        ticket = repo.create_ticket(project="p", issue_number=n + 1, now=_T0)
        repo.open_state_instance(
            ticket_id=ticket.id,
            state_name="Planning",
            sequence=1,
            now=_T0,
        )
    return repo, db_path


def test_take_snapshot_passes_integrity_check(tmp_path: Path) -> None:
    """The snapshot file must decompress to a SQLite that passes
    ``PRAGMA integrity_check`` AND show the same row counts as the
    live DB at snapshot time."""
    repo, db_path = _seed_live_db(tmp_path)
    backups = tmp_path / "backups"

    snapshot_path = take_snapshot(repo.connection, backups, now=_T0)
    assert snapshot_path.exists()
    assert snapshot_path.parent == backups

    # Decompress and check.
    decompressed = tmp_path / "decompressed.sqlite"
    with gzip.open(snapshot_path, "rb") as gz_f, open(decompressed, "wb") as out_f:
        out_f.write(gz_f.read())

    check_conn = sqlite3.connect(decompressed)
    try:
        row = check_conn.execute("PRAGMA integrity_check").fetchone()
        assert row == ("ok",)
        live_ticket_count = repo.connection.execute(
            "SELECT COUNT(*) FROM tickets",
        ).fetchone()[0]
        live_state_count = repo.connection.execute(
            "SELECT COUNT(*) FROM state_instances",
        ).fetchone()[0]
        snap_ticket_count = check_conn.execute(
            "SELECT COUNT(*) FROM tickets",
        ).fetchone()[0]
        snap_state_count = check_conn.execute(
            "SELECT COUNT(*) FROM state_instances",
        ).fetchone()[0]
        assert snap_ticket_count == live_ticket_count
        assert snap_state_count == live_state_count
        assert snap_ticket_count >= 3
    finally:
        check_conn.close()


def test_take_snapshot_filename_is_sortable_iso(tmp_path: Path) -> None:
    """Filename must match ``foreman-YYYYMMDDTHHMMSSZ.sqlite.gz`` so
    string-ordering equals time-ordering."""
    import re
    repo, _ = _seed_live_db(tmp_path)
    backups = tmp_path / "backups"
    snapshot_path = take_snapshot(repo.connection, backups, now=_T0)
    assert re.match(
        r"^foreman-\d{8}T\d{6}Z\.sqlite\.gz$", snapshot_path.name,
    )


def _touch(dst_dir: Path, ts: dt.datetime) -> Path:
    """Create an empty snapshot-shaped file at ``ts``. The pruner
    only reads timestamps from the filename, never from mtime, so
    an empty file is sufficient."""
    name = f"foreman-{ts.strftime('%Y%m%dT%H%M%SZ')}.sqlite.gz"
    path = dst_dir / name
    path.write_bytes(b"")
    return path


def test_prune_keeps_hourly_then_daily_then_weekly(tmp_path: Path) -> None:
    """Tier algorithm behavior, asserted with explicit per-tier sets.

    Construction:
    - 5 files within last hour → all 5 in hourly tier; under cap=24.
    - 3 files spanning 2 distinct UTC calendar days inside
      [now-7d, now-24h] → exactly 2 daily survivors (1 per bucket;
      within cap=7).
    - 4 files spanning 2 distinct Monday-UTC weeks inside
      [now-28d, now-7d] → exactly 2 weekly survivors (1 per bucket;
      within cap=4).
    - 1 file older than now - 28d → tier 4, always pruned.

    Total expected: 5 + 2 + 2 = 9 survivors, 1+1+2+1 = 5 deletions
    (1 dropped daily duplicate + 2 dropped weekly duplicates + 1
    tier-4 file = 4; ah wait — 3-2=1 daily, 4-2=2 weekly, 1 tier-4
    = 4 deletions. Let me recompute the assertion in the test).
    """
    dst = tmp_path / "backups"
    dst.mkdir()
    now = _T0

    # Tier 1: 5 files within last hour. Hourly cap=24, so all survive.
    hourly_files = [
        _touch(dst, now - dt.timedelta(minutes=m))
        for m in (5, 15, 25, 35, 45)
    ]

    # Tier 2: 3 files across 2 UTC calendar days inside [now-7d, now-24h].
    # Day A = 2026-06-12: two files at 06:00 and 04:00 (offsets 30h, 32h).
    # Day B = 2026-06-11: one file at 06:00 (offset 54h).
    # Daily bucketing keeps 1 per UTC day → 2 survivors.
    daily_day_a_keep = _touch(
        dst,
        dt.datetime(2026, 6, 12, 6, 0, 0, tzinfo=dt.UTC),
    )
    daily_day_a_drop = _touch(
        dst,
        dt.datetime(2026, 6, 12, 4, 0, 0, tzinfo=dt.UTC),
    )
    daily_day_b_keep = _touch(
        dst,
        dt.datetime(2026, 6, 11, 6, 0, 0, tzinfo=dt.UTC),
    )

    # Tier 3: 4 files across 2 Monday-UTC weeks inside [now-28d, now-7d].
    # Now=2026-06-13 (Sat). horizon_daily = now-7d = 2026-06-06 12:00.
    # horizon_weekly = now-28d = 2026-05-16 12:00.
    # Week of 2026-06-01 (Mon): 06-01..06-07. Both 06-04 and 06-05
    # land in this week.
    # Week of 2026-05-25 (Mon): 05-25..05-31. Both 05-26 and 05-28
    # land in this week.
    # Both weeks are inside [05-16 12:00, 06-06 12:00] → tier 3.
    # Weekly bucketing keeps 1 per Monday-UTC week → 2 survivors.
    weekly_w1_keep = _touch(
        dst,
        dt.datetime(2026, 6, 5, 12, 0, 0, tzinfo=dt.UTC),
    )
    weekly_w1_drop = _touch(
        dst,
        dt.datetime(2026, 6, 4, 12, 0, 0, tzinfo=dt.UTC),
    )
    weekly_w2_keep = _touch(
        dst,
        dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC),
    )
    weekly_w2_drop = _touch(
        dst,
        dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=dt.UTC),
    )

    # Tier 4: 1 file older than 28d. Always pruned.
    too_old = _touch(
        dst,
        dt.datetime(2026, 5, 1, 12, 0, 0, tzinfo=dt.UTC),
    )

    deleted = prune_snapshots(
        dst, now=now,
        retention=RetentionPolicy(hourly=24, daily=7, weekly=4),
    )
    after = set(dst.iterdir())

    expected_survivors = {
        *hourly_files,
        daily_day_a_keep, daily_day_b_keep,
        weekly_w1_keep, weekly_w2_keep,
    }
    expected_deletions = {
        daily_day_a_drop,
        weekly_w1_drop, weekly_w2_drop,
        too_old,
    }
    assert after == expected_survivors
    assert set(deleted) == expected_deletions


def test_prune_leaves_unparseable_filenames_alone(tmp_path: Path) -> None:
    """A file whose name doesn't parse as a snapshot timestamp must
    survive the prune untouched — operators drop manual backups in
    this dir occasionally."""
    dst = tmp_path / "backups"
    dst.mkdir()
    _touch(dst, _T0 - dt.timedelta(hours=1))
    manual = dst / "manual-backup.gz"
    manual.write_bytes(b"manual")

    prune_snapshots(
        dst, now=_T0,
        retention=RetentionPolicy(hourly=24, daily=7, weekly=4),
    )
    assert manual.exists(), "manual-backup.gz must survive prune"


def test_scheduler_tick_respects_interval(tmp_path: Path) -> None:
    """``tick()`` snapshots on first call always; on subsequent calls
    only when ``interval_seconds`` has elapsed."""
    repo, _ = _seed_live_db(tmp_path)
    backups = tmp_path / "backups"
    bus = EventBus()
    clock_now = [_T0]

    sched = BackupScheduler(
        src_conn=repo.connection,
        dst_dir=backups,
        interval_seconds=3600,
        retention=RetentionPolicy(),
        clock=lambda: clock_now[0],
        bus=bus,
    )

    # First call always fires.
    first = sched.tick()
    assert first is not None and first.exists()

    # Advance 30 minutes — should NOT snapshot.
    clock_now[0] = _T0 + dt.timedelta(minutes=30)
    second = sched.tick()
    assert second is None

    # Advance another 31 minutes (>1h total) — should snapshot.
    clock_now[0] = _T0 + dt.timedelta(minutes=61)
    third = sched.tick()
    assert third is not None and third.exists()
    assert third != first


def test_scheduler_disabled_returns_none(tmp_path: Path) -> None:
    """``BackupScheduler.from_config(BackupConfig(enabled=False))``
    returns the ``_DisabledBackupScheduler`` sentinel; ``tick()``
    returns None and writes no files."""
    repo, _ = _seed_live_db(tmp_path)
    bus = EventBus()
    backups = tmp_path / "backups"

    sched = BackupScheduler.from_config(
        BackupConfig(enabled=False, dir=str(backups)),
        src_conn=repo.connection,
        bus=bus,
    )
    assert isinstance(sched, _DisabledBackupScheduler)
    assert sched.tick() is None
    assert not backups.exists() or not any(backups.iterdir())


def test_scheduler_swallows_take_snapshot_error_and_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``take_snapshot`` raises ``OSError("disk full")`` the
    scheduler must NOT crash; it must publish a ``BackupFailedEvent``
    on the bus with ``phase='snapshot'`` and the truncated reason."""
    repo, _ = _seed_live_db(tmp_path)
    backups = tmp_path / "backups"
    bus = EventBus()
    captured: list = []
    bus.subscribe(captured.append)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "foreman.v4.state_backup.take_snapshot", _boom,
    )

    sched = BackupScheduler(
        src_conn=repo.connection,
        dst_dir=backups,
        interval_seconds=3600,
        retention=RetentionPolicy(),
        clock=lambda: _T0,
        bus=bus,
    )
    result = sched.tick()
    assert result is None  # scheduler did not crash and returned None
    failures = [e for e in captured if isinstance(e, BackupFailedEvent)]
    assert len(failures) == 1
    assert failures[0].phase == "snapshot"
    assert "disk full" in failures[0].reason
    # No BackupTakenEvent on failure.
    takes = [e for e in captured if isinstance(e, BackupTakenEvent)]
    assert takes == []
