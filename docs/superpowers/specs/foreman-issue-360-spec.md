# Spec: periodic SQLite snapshot to `~/.foreman/backups` with retention (issue #360)

## Goal
Foreman's authoritative SQLite state (`/foreman/state/foreman.sqlite` on the
container's `foreman-state` named volume) currently has zero backups; a WSL2
disk failure, accidental `docker compose down -v`, or partial-write
corruption would erase every ticket's transition history across every
registered project. This spec adds a daemon-internal scheduler that takes
online `sqlite3.Connection.backup()`-based snapshots, writes them gzip-
compressed to a host-bind-mounted `~/.foreman/backups/` directory (outside
the `foreman-state` volume so it survives `docker compose down -v`),
enforces tier-based retention, and ships a `foreman restore` CLI command
plus a RUNBOOK section so an operator can recover from a single-snapshot
file in minutes. Tracks
[foreman#360](https://github.com/jeffrichley/foreman/issues/360).

## Acceptance criteria
- [ ] A new module
  `packages/foreman/src/foreman/v4/state_backup.py` exposes three pure-ish
  surfaces: `take_snapshot(src_conn: sqlite3.Connection, dst_dir: Path,
  *, now: dt.datetime) -> Path` (writes one gzipped snapshot file, returns
  the path); `prune_snapshots(dst_dir: Path, *, now: dt.datetime,
  retention: RetentionPolicy) -> list[Path]` (returns the list of deleted
  paths); and a `BackupScheduler` class with constructor
  `BackupScheduler(*, src_conn, dst_dir, interval_seconds: int,
  retention: RetentionPolicy, clock: Callable[[], dt.datetime])` and one
  method `tick() -> Path | None` (returns the new snapshot's path on the
  ticks where one was taken; returns None otherwise — including
  `enabled=False`, see below).
- [ ] `take_snapshot` MUST use the stdlib
  `sqlite3.Connection.backup(target, *, pages=-1, sleep=0.25)` API
  against an open destination `sqlite3.connect(<tempfile>)`, then close
  both connections, then `gzip` the temp file into the final
  `dst_dir / f"foreman-{now.strftime('%Y%m%dT%H%M%SZ')}.sqlite.gz"`,
  then delete the temp file. The reason `.backup` is load-bearing (not
  `shutil.copyfile` or `cp`) is documented in the module docstring:
  WAL mode plus N concurrent worker-pool writers means a naïve file
  copy can capture a torn write or a WAL-segment-without-checkpoint
  state that fails `PRAGMA integrity_check` on restore. Match the
  shape the existing `EventArchiveObserver` uses in
  `packages/foreman/src/foreman/v4/observers/event_archive.py:60` —
  pass the shared `SqliteTicketRepository.connection` handle (exposed
  at `packages/foreman/src/foreman/v4/sqlite_repository.py:125-136`)
  as `src_conn` so the snapshot rides the same connection RLock and
  the documented "additional writes from outside the repository are
  serialized by the SQLite engine layer" guarantee holds.
- [ ] `RetentionPolicy` is a frozen dataclass in the same module with
  fields `hourly: int = 24`, `daily: int = 7`, `weekly: int = 4`. The
  three integers correspond directly to the issue's stated retention
  goal ("Last 24 hourly snapshots, Last 7 daily snapshots, Last 4
  weekly snapshots" → ~35 files total).
- [ ] `prune_snapshots` glob-matches `foreman-*.sqlite.gz` in
  `dst_dir`, parses each timestamp from the filename (filenames whose
  timestamp does not parse are LEFT ALONE — humans drop manual
  backups in this dir occasionally and the pruner must not eat them),
  and applies the tier policy as follows. Let `now` be the passed
  clock value; classify each parseable file by age:
  1. Files newer than `now - 24h`: keep the most-recent
     `retention.hourly` of them (sorted by timestamp DESC). Anything
     beyond that count is pruned.
  2. Files between `now - 24h` and `now - 7d`: bucket into
     calendar-day windows (in UTC). Within each day-window keep ONLY
     the most-recent file; older files in the same window are pruned.
     Then keep at most `retention.daily` such daily-survivors;
     anything beyond is pruned.
  3. Files between `now - 7d` and `now - 28d`: bucket into 7-day
     windows (week starts Monday UTC, mirroring ISO 8601 week
     semantics). Within each week-window keep ONLY the most-recent
     file; older files in the same window are pruned. Then keep at
     most `retention.weekly` such weekly-survivors.
  4. Files older than `now - 28d`: all pruned.
  Return the list of paths that were deleted, so the caller can
  structured-log them. Pruning runs AFTER each successful snapshot;
  see `BackupScheduler.tick()` below.
- [ ] `BackupScheduler.tick()` reads `self._clock()` once, checks
  whether `(now - self._last_snapshot_at).total_seconds() >=
  self._interval_seconds` (where `_last_snapshot_at` is initialized to
  `None` so the first call always snapshots), and if so calls
  `take_snapshot(...)` followed by `prune_snapshots(...)`. The
  scheduler MUST swallow `OSError` / `sqlite3.Error` raised by
  `take_snapshot` or `prune_snapshots` and structured-log the failure
  via a NEW event class (see `BackupFailedEvent` below) — a transient
  disk-full or read-only-FS failure must not crash the daemon loop.
  Successful snapshots emit a `BackupTakenEvent` carrying
  `(path: Path, size_bytes: int, pruned: list[Path])`.
- [ ] `BackupConfig` is added to `packages/foreman/src/foreman/v4/config.py`
  as a `pydantic.BaseModel` with `model_config =
  ConfigDict(extra="forbid")` (same shape as the existing
  `OperatorConfig` / `AppsConfig` blocks) and fields:
  * `enabled: bool = True`
  * `dir: str = "/foreman/backups"` (container-internal, matches the
    bind mount in `docker-compose.yml` added below)
  * `interval_seconds: int = Field(default=3600, ge=60)` — the
    `ge=60` floor is runaway-defense: a misconfigured interval of
    `0` would snapshot every daemon tick (default 30s) and fill
    disk in minutes.
  * `retention_hourly: int = Field(default=24, ge=0)`
  * `retention_daily: int = Field(default=7, ge=0)`
  * `retention_weekly: int = Field(default=4, ge=0)`
  `V4Config` gains `backup: BackupConfig = Field(default_factory=BackupConfig)`
  (defaulted, NOT required, so existing operator configs without a
  `[backup]` block continue to load — backups default to on with the
  documented schedule). When `backup.enabled` is False the
  `BackupScheduler` constructor short-circuits to a sentinel whose
  `tick()` always returns None — the daemon wiring becomes a single
  unconditional `self._backup_scheduler.tick()` call site (Approach:
  "make the right thing easy" — no None-check in the hot path).
- [ ] `bootstrap_cli_context` in
  `packages/foreman/src/foreman/v4/bootstrap.py` constructs a
  `BackupScheduler` from `config.backup` + `repo.connection` and
  passes it to `Daemon(..., backup_scheduler=...)`. When
  `config.backup.enabled` is False, bootstrap still constructs a
  scheduler (the no-op sentinel form) so `Daemon` has a uniform
  attribute to call.
- [ ] `Daemon.__init__` in
  `packages/foreman/src/foreman/v4/daemon.py` gains a
  `backup_scheduler: BackupScheduler | None = None` kwarg (defaulted so
  existing test fixtures that construct `Daemon(...)` directly stay
  green; bootstrap always passes one in production). `Daemon.tick_once()`
  calls `self._backup_scheduler.tick()` (if non-None) AFTER
  `self._pool.tick()` and BEFORE the bounded-drain `while` loop — the
  snapshot work is in-process, single-threaded, and runs on the same
  thread as the tick; the WAL-mode online backup does not block
  worker-pool writers but burns a few hundred ms per snapshot, which
  is negligible against the 30s default `tick_seconds`.
- [ ] `BackupTakenEvent` and `BackupFailedEvent` are added in
  `packages/foreman/src/foreman/v4/events.py` as frozen dataclasses
  with the standard `Event` envelope (`at: dt.datetime`) PLUS:
  * `BackupTakenEvent`: `path: str`, `size_bytes: int`,
    `pruned_count: int`.
  * `BackupFailedEvent`: `phase: Literal["snapshot", "prune"]`,
    `reason: str` (the str() of the swallowed exception, truncated
    to 500 chars to keep the log line tractable).
  Note: these two events do NOT carry `ticket_id` / `instance_id` /
  `state_name` / `sequence` — they are daemon-level, not state-machine
  events. The base `Event` class today is hard-coded with those
  ticket-scoped fields (see existing event types in `events.py`);
  add a sibling parent class `DaemonEvent` (also frozen, carries
  `at: dt.datetime` only) that `BackupTakenEvent` /
  `BackupFailedEvent` inherit from. Document the split in
  `events.py`'s top-of-file docstring.
- [ ] `StructuredLogObserver._EVENT_NAMES` in
  `packages/foreman/src/foreman/v4/observers/structured_log.py`
  gains two new entries:
  `BackupTakenEvent: ("backup_taken", logging.INFO)` and
  `BackupFailedEvent: ("backup_failed", logging.ERROR)`. The
  observer's `__call__` hard-codes per-event-type field emission via
  `isinstance` branches (precedent: existing arms in the same file);
  add two new branches that write the event-specific payload fields
  (`path`/`size_bytes`/`pruned_count` for taken;
  `phase`/`reason` for failed). The observer's existing assumption
  that every event has `ticket_id` etc. needs widening — add a
  guard at the top of `__call__` that emits a minimal envelope
  (`{at, event}`) when the event is a `DaemonEvent` and skips the
  ticket-scoped field lookup entirely. Without this widening the
  observer would raise `AttributeError` on the new events.
- [ ] `EventArchiveObserver` in
  `packages/foreman/src/foreman/v4/observers/event_archive.py` is
  modified to NO-OP on `DaemonEvent` subclasses — the `events` table
  schema (see `schema.sql:51-60`) requires `ticket_id INTEGER NOT
  NULL`, so trying to insert a daemon-level event would violate
  the constraint and (since the EventBus isolates observer
  exceptions per the existing `EventArchiveObserver` docstring at
  line 6) silently corrupt the audit trail. Add an early
  `if isinstance(event, DaemonEvent): return` guard at the top of
  `__call__`. The structured-log JSONL is the durable record for
  daemon-level events; the events SQL table stays ticket-scoped.
- [ ] `docker-compose.yml` gains a new bind mount on the `daemon`
  service: `${HOME}/.foreman/backups:/foreman/backups` (host →
  container). The mount is NOT a named volume — using a bind mount
  is load-bearing because (a) `docker compose down -v` deletes
  named volumes (the explicit failure scenario from the issue body)
  but leaves bind-mounted host directories untouched, and (b)
  putting backups on the same `foreman-state` volume they're
  protecting defeats the purpose. Add a one-line comment above the
  new mount naming foreman#360.
- [ ] `docker/foreman/config.toml.template` gains a `[backup]`
  block:
  ```toml
  [backup]
  enabled = true
  dir = "/foreman/backups"
  interval_seconds = 3600
  retention_hourly = 24
  retention_daily = 7
  retention_weekly = 4
  ```
  placed after `[daemon]` and before `[apps]` (alphabetical by
  block name keeps the file scannable). The values here mirror the
  pydantic defaults; the block is written explicitly so operators
  can see + tune the schedule without having to read the source.
- [ ] A new `foreman restore <snapshot-file>` CLI subcommand in a
  new file `packages/foreman/src/foreman/v4/cli/restore.py`
  registered in `packages/foreman/src/foreman/v4/cli/__init__.py`
  via `app.command("restore")(cmd_restore)`. Signature:
  `cmd_restore(snapshot_file: Path = typer.Argument(...))`.
  Behavior, in order:
  1. Resolve `db_path` from
     `ctx.obj.config.db_path` (CliContext already carries the
     config; existing pattern at `cli/init.py:65-69`).
  2. Refuse if the daemon is alive: check
     `~/.foreman/v4/daemon.pid` (the path constant lives at
     `cli/daemon.py:36` as `_PID_PATH` — import it; do not
     duplicate the literal). If the PID file exists AND
     `_is_pid_alive(pid)` returns True (same helper at
     `cli/daemon.py:46-65` — re-export from the module so
     `restore.py` can import it), print
     "daemon is running (pid <N>); stop it first with
     `foreman daemon stop` before restoring" to stderr and exit
     code 1. The daemon's writers would race the restore swap
     and corrupt either the live DB or the restored one.
  3. Validate `snapshot_file` exists and is readable; exit 1
     otherwise with a clean error message.
  4. Take a "pre-restore" snapshot of the current live DB
     (calls `take_snapshot(live_conn, dst_dir=db_path.parent, ...)`)
     and rename it to `db_path.with_suffix(f".pre-restore-
     {now.strftime('%Y%m%dT%H%M%SZ')}.sqlite.gz")` — one-step
     undo for the "I restored the wrong file" case. Print the
     pre-restore path to stdout so the operator can grep their
     terminal scrollback.
  5. If `snapshot_file.name.endswith(".gz")`, decompress to a
     tempfile alongside `db_path` (same dir, so the eventual
     `os.replace` is atomic on POSIX); otherwise treat the
     input as a raw sqlite file and copy it to the tempfile.
  6. Open the tempfile with `sqlite3.connect(tempfile)` and run
     `PRAGMA integrity_check;`. If the result is anything other
     than the single row `("ok",)`, delete the tempfile, print
     the failure detail, exit 1. The pre-restore snapshot from
     step 4 stays — the operator can still recover.
  7. `os.replace(tempfile, db_path)` — atomic on POSIX; on
     Linux this is what the container runs.
  8. Print "restored <snapshot_file> → <db_path> (pre-restore
     saved as <pre-restore-path>); start the daemon with
     `docker compose up -d daemon`" and exit 0.
- [ ] `cli/daemon.py:_is_pid_alive` and `cli/daemon.py:_PID_PATH`
  stay module-private today; this spec promotes them by adding
  `_is_pid_alive` to a module-level `__all__` (or, if no
  `__all__` exists, just stops the leading underscore — rename
  to `is_pid_alive` and `PID_PATH` and update the two existing
  call sites in the same file). Rationale: the `foreman restore`
  command needs the same liveness check; duplicating the helper
  would drift. Pick the simpler shape (rename + update two call
  sites) — there is no public API contract to preserve.
- [ ] `cmd_restore` is intentionally a host-side-or-container
  command: when the operator stops the container with
  `docker compose stop daemon` and then runs
  `docker compose run --rm daemon foreman restore <file>`, the
  one-off container spawns with the same `foreman-state` named
  volume AND the same `~/.foreman/backups` bind mount, so
  `db_path` and `snapshot_file` are both resolvable inside the
  one-off container. Document this invocation shape in the
  RUNBOOK section (see below) verbatim.
- [ ] New test file
  `packages/foreman/tests/v4/test_state_backup.py` covering, at
  minimum:
  * `test_take_snapshot_passes_integrity_check`: seed a
    `SqliteTicketRepository.at_path(tmp_path / "live.db")` with
    a few tickets + state-instance rows; call `take_snapshot`;
    decompress the result; assert
    `sqlite3.connect(decompressed).execute("PRAGMA
    integrity_check").fetchone() == ("ok",)` AND the row counts
    in `tickets` + `state_instances` match the live DB at
    snapshot time.
  * `test_take_snapshot_filename_is_sortable_iso`: assert the
    filename matches the regex
    `^foreman-\d{8}T\d{6}Z\.sqlite\.gz$` so ordering by name
    matches ordering by time.
  * `test_prune_keeps_hourly_then_daily_then_weekly`: seed the
    dir with 60 snapshots spaced 1 hour apart over the last 60
    hours and an additional 30 snapshots spaced 1 day apart
    over the last 30 days; call `prune_snapshots(...,
    retention=RetentionPolicy(24, 7, 4))`; assert exactly
    `24 + 7 + 4 = 35` files survive AND the surviving files are
    the most-recent-per-tier per the algorithm above.
  * `test_prune_leaves_unparseable_filenames_alone`: seed the
    dir with one snapshot AND a file named `manual-backup.gz`
    (no parseable timestamp); call `prune_snapshots`; assert
    `manual-backup.gz` still exists.
  * `test_scheduler_tick_respects_interval`: stub clock to
    `t0`, construct `BackupScheduler(interval_seconds=3600)`,
    call `tick()` (asserts snapshot taken — first call always
    fires), advance clock by 30 minutes, call `tick()` (asserts
    None returned — too soon), advance clock by 31 more
    minutes (>= 1 hour total), call `tick()` (asserts second
    snapshot taken).
  * `test_scheduler_tick_disabled_returns_none`: construct
    `BackupScheduler` from a `BackupConfig(enabled=False)`;
    `tick()` returns None and writes no files.
  * `test_scheduler_swallows_take_snapshot_error_and_logs`:
    patch `take_snapshot` to raise `OSError("disk full")`;
    call `tick()`; assert the call returns None (no crash) AND
    a `BackupFailedEvent(phase="snapshot", reason="disk
    full")` was published via the injected event bus.
- [ ] New test file `packages/foreman/tests/v4/cli/test_restore.py`
  covering, at minimum:
  * `test_restore_round_trip`: take a snapshot of a live DB,
    move the live DB aside (simulating "lost the volume"), run
    `cmd_restore` against the snapshot, assert the resulting
    DB at `db_path` passes integrity check AND has the same
    ticket/state-instance contents as the original.
  * `test_restore_refuses_when_daemon_alive`: create a PID file
    pointing at `os.getpid()` (which is by definition alive);
    invoke `cmd_restore`; assert exit code 1 AND the live DB
    was not modified.
  * `test_restore_refuses_corrupt_snapshot`: hand-craft a
    "snapshot" file containing random bytes; invoke
    `cmd_restore`; assert exit code 1 AND the live DB was not
    modified AND the pre-restore snapshot from step 4 is gone
    too (the pre-restore is taken BEFORE the integrity check,
    so the test asserts the operator can still find their
    pre-restore file on disk — see acceptance criterion above).
- [ ] `packages/foreman/tests/v4/test_config.py` is extended with
  three new tests:
  * `test_backup_block_defaulted_when_absent`: load a TOML with
    no `[backup]` block; assert
    `config.backup.enabled is True` (defaulted) and the other
    fields match the documented defaults.
  * `test_backup_block_validated_when_present`: load a TOML
    with a `[backup]` block that sets `interval_seconds = 30`;
    assert `ValidationError` is raised (the `ge=60` floor
    fires).
  * `test_backup_block_extras_forbidden`: load a TOML with a
    `[backup]` block carrying an unknown field; assert
    `ValidationError` (extra-forbid mirrors the other config
    blocks).
- [ ] `packages/foreman/tests/v4/test_daemon.py` gains
  `test_tick_once_calls_backup_scheduler`: construct a `Daemon`
  with a stub `BackupScheduler` whose `tick()` is a `MagicMock`;
  call `daemon.tick_once()`; assert the stub's `tick()` was
  called exactly once. A second test
  `test_tick_once_runs_when_backup_scheduler_is_none` constructs
  the daemon WITHOUT a scheduler (defaulted kwarg) and asserts
  `tick_once()` does not raise.
- [ ] `docs/RUNBOOK.md` gains a new section titled "Backups and
  restoration" placed BETWEEN "Daily operations" and "Recovery:
  daemon won't start" (mirrors the layout pattern used by
  foreman#361's spec for the "Provider transient failures"
  section). The section MUST document:
  * What backups exist (`~/.foreman/backups/foreman-*.sqlite.gz`)
    and the schedule (default hourly, 24/7/4 retention totalling
    ~35 files).
  * How to verify a backup is non-corrupt:
    ```bash
    gunzip -c ~/.foreman/backups/foreman-<ts>.sqlite.gz > /tmp/check.sqlite
    sqlite3 /tmp/check.sqlite "PRAGMA integrity_check;"
    # should print: ok
    ```
  * The restore procedure verbatim:
    ```bash
    docker compose stop daemon
    # one-off container mounts the same volumes/binds as `up`:
    docker compose run --rm daemon \
        foreman restore /foreman/backups/foreman-<ts>.sqlite.gz
    docker compose up -d daemon
    ```
  * The one-step undo: the pre-restore snapshot is written next
    to the live DB as `foreman.pre-restore-<ts>.sqlite.gz`;
    rename it back into place with another `foreman restore`
    call against that file.
  * Tuning: how to change `interval_seconds` /
    `retention_*` in `config.toml.template` (operators with
    bigger disks can keep more; operators on small WSL2 disks
    can keep fewer or turn backups off entirely with
    `enabled = false`).

## Approach
**Pattern naming (Decision 4 — calibrated lens).** No GoF pattern
fits cleanly. The shape is straightforward "periodic job on the
existing tick loop". Two Google engineering principles do apply:

1. **Single Responsibility (SRP).** Three distinct functions live
   in `state_backup.py`: `take_snapshot` knows ONLY how to write
   one snapshot; `prune_snapshots` knows ONLY how to apply the
   retention algorithm; `BackupScheduler` knows ONLY how to decide
   when to call the other two. The Daemon doesn't know what a
   "snapshot" is; the scheduler doesn't know how `sqlite3.backup`
   works; the pruner doesn't know about clocks beyond the one
   `now` argument the caller passes. This is what lets each piece
   be tested in isolation — `take_snapshot` against a real
   SqliteRepository, `prune_snapshots` against a `tmp_path` with
   `os.utime`-massaged mtimes, `BackupScheduler` against a stub
   `take_snapshot`.
2. **"Make the right thing easy".** One config block
   (`[backup]`), one CLI command (`foreman restore`), one
   on-disk directory (`~/.foreman/backups/`), one RUNBOOK
   section. An operator who needs to recover from "the WSL2 disk
   ate everything" has exactly one cluster of docs + commands to
   read. The disabled-scheduler sentinel (constructor short-
   circuits when `enabled=False`) eliminates the alternative —
   threading `if backup_scheduler is not None:` through
   `tick_once` — that would drift the moment a maintenance task
   reorders the tick body.

**Why a daemon-internal scheduler vs. host cron or a sidecar
container.** The issue body explicitly leaves the shape open
("cron on the host vs. internal scheduler tick vs. a sidecar
container"). All three would satisfy the functional criteria;
the trade-offs:

- *Host cron* requires per-operator shell config (cron on Linux,
  Task Scheduler on Windows, launchd on macOS). Foreman targets
  multi-OS operators (the project README, Windows-WSL2-via-Docker
  posture). Host cron means N versions of the same config and N
  failure modes operators have to learn.
- *Sidecar container* is portable but adds an image (a second
  Dockerfile or a shared image with two entrypoints) and a
  second container in `docker-compose.yml`. Two failure
  surfaces (the daemon container AND the backup container) for
  what is structurally one feature.
- *Internal scheduler tick* runs in the same process the operator
  is already running. Zero new ops surface. Reuses the existing
  `Daemon.tick_once()` rhythm; the scheduler decides whether
  enough wall-clock time has elapsed to take a new snapshot.
  The cost is one extra in-process call per 30-second tick (a
  fast no-op when not due) plus a few hundred ms once per hour
  when due. Negligible.

**Why bind-mount the host's `~/.foreman/backups/` instead of
adding another named volume.** This is load-bearing for the
specific failure mode the issue cites. `docker compose down -v`
deletes named volumes — that's the *whole point* of the `-v`
flag. If backups landed on a `foreman-backups` named volume,
`down -v` would delete them in the same gesture that deletes
the thing they're protecting. A host bind mount survives
`down -v` because Docker only owns the *mount*, not the host
directory it points at. The operator's `~/.foreman/` directory
is also already half-promoted to foreman state surface (it
holds `~/.foreman/keys/*.pem` consumed via Compose secrets in
the existing `docker-compose.yml:71-83` block); adding
`~/.foreman/backups/` to it is consistent.

**Why `sqlite3.Connection.backup()` is load-bearing (not `cp`
or `shutil.copyfile`).** Foreman's repo runs in WAL mode (see
`sqlite_repository.py:102` — `PRAGMA journal_mode=WAL`) so
writers don't block readers. The flip side: at any moment the
on-disk `foreman.sqlite` file plus its `-wal` and `-shm`
sidecars together represent the database state. A plain file
copy that grabs `foreman.sqlite` without atomically grabbing
the WAL segment captures a stale snapshot of committed
transactions; a copy that grabs both with no synchronization
can capture a checkpoint mid-flight and produce a file that
fails `PRAGMA integrity_check`. The
`sqlite3.Connection.backup(target, pages=-1, sleep=0.25)` API
talks to the source DB's transaction layer directly, copies
pages atomically without blocking writers, and produces a
verifiably-consistent target. This is the only safe
recipe for a hot SQLite backup, and the issue body calls it
out by name ("MUST use SQLite's online `.backup`").

**Why the snapshot is gzipped immediately.** Two reasons. First,
SQLite files compress 5-7× because page padding + JSON outcome
payloads + transition history are highly repetitive. ~35 files
× a few MB × ~5× compression keeps the on-disk footprint in
the low tens of MB — small enough that a WSL2 host with a
modest free-disk budget doesn't notice. Second, atomicity:
gzip writes its output and either succeeds or doesn't; we
delete the uncompressed tempfile only on success, so a partial
snapshot never appears in the backups directory. The
pruner's `foreman-*.sqlite.gz` glob is implicitly the
"successful snapshots" filter.

**Why a tier-based retention algorithm vs. simpler N-most-recent.**
"Keep the last 35 snapshots, no other policy" would mean that
once a backup is older than 35 hours it disappears — the
operator would have no week-old or month-old recovery point.
Tier-based gives the operator a smooth horizon: hourly for the
last day (catches "I just ran `down -v` two hours ago"), daily
for the last week (catches "I noticed last Tuesday's
configuration error today"), weekly for the last month
(catches "I want to compare this week's state to four weeks
ago"). It's the same shape conventional backup tools use
(Time Machine, restic snapshot-prune policies) and matches the
issue's stated retention goal verbatim.

**Why `foreman restore` lives in `cli/restore.py` as a separate
file, not added to `cli/mutations.py`.** The mutations group is
about ticket-level operations (hold, resume, retry, skip, drop,
set-state, enqueue, reset). Restore is a daemon-level operation
that swaps an entire DB file and refuses to run while the
daemon is alive. Co-locating it with the ticket mutations would
violate SRP and would force the mutations module to import the
PID-file plumbing from `cli/daemon.py`. A separate file makes
the import graph linear (`restore.py` imports from `daemon.py`,
nothing imports `restore.py` except `cli/__init__.py` to
register it).

**Why pre-restore snapshots vs. "trust the operator".** The
restore command's most-dangerous failure mode is "operator
runs `foreman restore` against the wrong file" — for example,
restoring last week's snapshot when intending to restore
yesterday's. Without the pre-restore snapshot, the live DB at
restore time is irretrievable. With it, the same `foreman
restore` command against the `.pre-restore-<ts>.sqlite.gz`
file is the one-step undo. Cost: one extra ~few-MB file per
restore. Benefit: making destructive operations recoverable is
exactly what the issue is about — extending the same
philosophy from "the daemon's state file" to "the operator's
restore action" is consistent.

**Why `enabled=False` returns a sentinel scheduler rather than
making the daemon attribute None.** The Daemon's `tick_once`
hot path runs once per `tick_seconds` (default 30s). A None
check there would (a) require every test that constructs a
Daemon to remember whether the field is None, (b) require the
production wiring to thread the bool through the same
indirection, (c) drift over time. A sentinel
`_DisabledBackupScheduler.tick()` that just `return None` is
one line of code and means the daemon's call site is
unconditional. Single shape, single test, no drift.

## Sub-requests (topologically sorted)
1. Add `BackupConfig` pydantic model + `backup: BackupConfig =
   Field(default_factory=BackupConfig)` field on `V4Config` in
   `packages/foreman/src/foreman/v4/config.py`. Extend
   `load_config` to forward the `[backup]` block when present
   (mirror the existing `if "apps" in raw:` shape at lines
   312–317).
2. Add `DaemonEvent` parent class + `BackupTakenEvent` +
   `BackupFailedEvent` in `packages/foreman/src/foreman/v4/events.py`.
3. Add `StructuredLogObserver` widening (the daemon-event guard)
   + two new `_EVENT_NAMES` entries +
   per-event-type branches in
   `packages/foreman/src/foreman/v4/observers/structured_log.py`.
4. Add `EventArchiveObserver` no-op guard for `DaemonEvent` in
   `packages/foreman/src/foreman/v4/observers/event_archive.py`.
5. Add the new module
   `packages/foreman/src/foreman/v4/state_backup.py` with
   `take_snapshot`, `RetentionPolicy`, `prune_snapshots`, and
   `BackupScheduler` (real + disabled sentinel via
   constructor short-circuit).
6. Rename `cli/daemon.py:_is_pid_alive` → `is_pid_alive` and
   `_PID_PATH` → `PID_PATH`; update the two existing in-file
   call sites. (Optional `__all__` addition; either shape is
   fine.)
7. Add `packages/foreman/src/foreman/v4/cli/restore.py` with
   `cmd_restore` per the acceptance shape, importing
   `is_pid_alive` + `PID_PATH` from the renamed symbols in
   step 6. Register the command in
   `packages/foreman/src/foreman/v4/cli/__init__.py` via
   `app.command("restore")(cmd_restore)`.
8. Wire `BackupScheduler` through `bootstrap_cli_context` in
   `packages/foreman/src/foreman/v4/bootstrap.py` (passes
   `config.backup` + `repo.connection` + an injected event
   bus) and into `Daemon(..., backup_scheduler=...)`.
9. Extend `Daemon.__init__` (new kwarg) and `Daemon.tick_once`
   (one call to `self._backup_scheduler.tick()`) in
   `packages/foreman/src/foreman/v4/daemon.py`.
10. Add the `~/.foreman/backups → /foreman/backups` bind mount
    to `docker-compose.yml`.
11. Add the `[backup]` block to
    `docker/foreman/config.toml.template`.
12. Write the unit + integration tests enumerated in
    Acceptance.
13. Add the RUNBOOK section.

## File-level changes
- `packages/foreman/src/foreman/v4/state_backup.py` — NEW:
  `RetentionPolicy`, `take_snapshot`, `prune_snapshots`,
  `BackupScheduler` (+ `_DisabledBackupScheduler` sentinel).
- `packages/foreman/src/foreman/v4/config.py` — add
  `BackupConfig` model; add `backup` field on `V4Config`;
  extend `load_config` to forward the `[backup]` block.
- `packages/foreman/src/foreman/v4/events.py` — add
  `DaemonEvent` parent + `BackupTakenEvent` +
  `BackupFailedEvent`.
- `packages/foreman/src/foreman/v4/observers/structured_log.py`
  — add `DaemonEvent` guard at top of `__call__`; add two
  `_EVENT_NAMES` entries; add two per-event-type
  `isinstance` branches for the new fields.
- `packages/foreman/src/foreman/v4/observers/event_archive.py`
  — add `isinstance(event, DaemonEvent): return` no-op guard
  at top of `__call__`.
- `packages/foreman/src/foreman/v4/daemon.py` — add
  `backup_scheduler: BackupScheduler | None = None` kwarg on
  `__init__`; store; call `self._backup_scheduler.tick()` from
  `tick_once()` after `self._pool.tick()` and before the
  bounded drain.
- `packages/foreman/src/foreman/v4/bootstrap.py` — construct
  `BackupScheduler` from `config.backup` + `repo.connection`
  + `bus`; pass through to `Daemon(...)`.
- `packages/foreman/src/foreman/v4/cli/__init__.py` — register
  `app.command("restore")(cmd_restore)`.
- `packages/foreman/src/foreman/v4/cli/daemon.py` — rename
  `_is_pid_alive` → `is_pid_alive` and `_PID_PATH` →
  `PID_PATH`; update the two existing call sites in the same
  file.
- `packages/foreman/src/foreman/v4/cli/restore.py` — NEW:
  `cmd_restore` per the acceptance shape.
- `docker-compose.yml` — add
  `${HOME}/.foreman/backups:/foreman/backups` bind mount on
  the `daemon` service (foreman#360 comment).
- `docker/foreman/config.toml.template` — add `[backup]`
  block between `[daemon]` and `[apps]`.
- `packages/foreman/tests/v4/test_state_backup.py` — NEW:
  the six tests enumerated above (take, filename, prune
  tiers, unparseable-leftalone, scheduler-interval,
  scheduler-disabled, scheduler-swallow).
- `packages/foreman/tests/v4/cli/test_restore.py` — NEW:
  round-trip, refuse-when-alive, refuse-corrupt.
- `packages/foreman/tests/v4/test_config.py` — append three
  backup-block tests (defaulted-when-absent, validated-
  when-present, extras-forbidden).
- `packages/foreman/tests/v4/test_daemon.py` — append
  `test_tick_once_calls_backup_scheduler` and
  `test_tick_once_runs_when_backup_scheduler_is_none`.
- `docs/RUNBOOK.md` — new "Backups and restoration" section
  placed between "Daily operations" and "Recovery: daemon
  won't start".

## Alternatives considered
1. **Host cron + `docker exec foreman-daemon sqlite3 ".backup
   /tmp/snap.db" && docker cp ...`.** Rejected: each operator
   has to write the cron config themselves (cron / Task
   Scheduler / launchd), and Foreman's stated multi-OS
   posture (Windows-WSL2-via-Docker per README) means three
   incompatible cron setups. Operator burden is what's wrong
   here — the feature being out of source control is what
   makes "did the backup actually run last night?" hard to
   answer.
2. **Sidecar `foreman-backup` container in docker-compose.yml.**
   Rejected: adds an image (or a shared image with a second
   entrypoint), a second container to monitor, a second log
   surface. Two failure modes for what is structurally one
   responsibility. Internal scheduler is strictly simpler.
3. **Background daemon thread (`threading.Timer` or a
   dedicated thread spinning on `time.sleep`).** Rejected:
   the existing `Daemon.tick_once()` rhythm already gives us
   a periodic wakeup; adding a second timing source means
   two places to test, two shutdown semantics, and a
   `threading.Event` join in `Daemon.shutdown()`. The
   scheduler-on-tick shape reuses everything the daemon
   already has.
4. **Replicate state to a second SQLite via
   `BEGIN IMMEDIATE; ... ATTACH; INSERT INTO target.tickets
   SELECT * FROM main.tickets; ...` row-by-row.** Rejected:
   defeats the WAL-mode online backup guarantee, requires
   schema-aware replication that has to grow every time we
   add a column (foreman#361 just added one), and the
   `sqlite3.Connection.backup()` API exists precisely to
   spare callers from writing this.
5. **Skip retention; let the disk grow unbounded.**
   Rejected: WSL2's virtual disk fills, the daemon's storage
   eventually wins a "low disk" event, and the operator
   debugs a cascade of "out of space" errors instead of a
   missing snapshot. The retention algorithm is small and
   well-tested in the snapshot world (Time Machine,
   restic); the cost of implementing it is low.

## Open questions
None. The retention algorithm, file naming, schedule, restore
procedure, and bind-mount shape are all directly traceable to
the issue body. One implementation-time judgment call: the
exact ISO 8601 vs. "Monday UTC" week boundary in step 3 of the
pruning algorithm is a minor decision and either reasonable
choice works; the spec picks Monday UTC for parity with ISO 8601
week semantics, and the test fixture's snapshots are spaced
1 hour / 1 day / 1 week so the exact boundary doesn't change
which files survive.

## Out of scope
- **Off-host backup (S3, B2, network drive, rsync target).**
  Explicitly out of scope per the issue body — local disk
  backup catches WSL2 corruption + `docker compose down -v`;
  off-host backup is a separate epic that should pick this
  spec's snapshot files as its source.
- **Continuous replication or hot standby.** Overkill for the
  current single-operator dogfood scale; the issue body calls
  this out as overkill explicitly.
- **Backing up `/foreman/repos/` (cloned project trees).**
  Reconstructable from GitHub via `git clone`; backing them
  up would multiply storage for zero recovery benefit.
- **Backing up `~/.foreman/keys/*.pem` (GitHub App private
  keys).** The operator already holds the canonical copies
  locally + GitHub stores the public half; backing them up
  inside Foreman's backup directory would conflate the
  daemon's state with the operator's secrets.
- **Encrypting snapshots at rest.** The host's
  `~/.foreman/backups/` inherits the host filesystem's
  permissions (operator-only on a personal machine).
  Encryption would belong in the same off-host-backup epic
  if foreman ever pushes snapshots somewhere shared.
- **Schema migration of restored snapshots.** A snapshot
  taken under schema vN cannot be restored into a daemon
  running schema vN+1 — the migration would have to run
  against the restored DB. This spec treats schema
  migration as a separate concern: snapshots are file-level
  artifacts; `SqliteTicketRepository.__init__` already runs
  `executescript(_SCHEMA.read_text())` on every open, which
  picks up additive changes for free. Non-additive
  migrations would need a `foreman migrate` companion to
  `foreman restore`, deferred to a future ticket.
- **Audit-log-style backup history.** Snapshots are
  self-describing (the timestamp is in the filename, the
  contents are inspectable with `sqlite3`); a separate
  "backup history" table would be redundant with what
  `ls ~/.foreman/backups/` already shows.
