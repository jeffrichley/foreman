# Spec: Postgres backup/restore DR story for `foreman-pg-data` volume (issue #434)

## Goal

Add a `pg_dump`-based backup story for the `foreman-pg-data` named volume — the production Postgres state store — closing the DR gap left by the SQLite backup system's removal. Backups write gzip-compressed logical dump files to a host bind-mounted `~/.foreman/backups/` directory (which survives `docker compose down -v`), enforce tiered retention (24 hourly / 7 daily / 4 weekly snapshots), and ship a `foreman restore` CLI command for single-command recovery. Tracks [foreman#434](https://github.com/jeffrichley/foreman/issues/434).

## Acceptance criteria

- [ ] **Dockerfile**: A new `RUN` step (after the existing system-deps block) adds the PostgreSQL PGDG apt repository and installs `postgresql-client-16`. This puts `pg_dump` and `psql` on PATH inside the daemon image at the correct major version to talk to the `postgres:16-alpine` sidecar. On `python:3.12-slim` (Debian Bookworm), the default apt `postgresql-client` resolves to v15, which cannot dump from a v16 server; the PGDG repo is required.

- [ ] **New event classes** in `packages/foreman/src/foreman/v4/events.py` — restore the two `DaemonEvent` subclasses removed by the SQLite kill:
  * `BackupTakenEvent(DaemonEvent)`: frozen dataclass, fields `path: str`, `size_bytes: int`, `pruned_count: int` (plus inherited `at: dt.datetime`).
  * `BackupFailedEvent(DaemonEvent)`: frozen dataclass, fields `phase: Literal["snapshot", "prune"]`, `reason: str` (plus inherited `at`).
  The `Event > DaemonEvent` hierarchy is already present in `events.py` (lines 40–79); these classes slot back in under `DaemonEvent` exactly as the rest of the type hierarchy documents.

- [ ] **`BackupConfig`** added to `packages/foreman/src/foreman/v4/config.py` as a `pydantic.BaseModel` with `model_config = ConfigDict(extra="forbid")`:
  * `enabled: bool = True`
  * `dir: str = "/foreman/backups"` — container-internal path, mapped by the bind mount below
  * `interval_seconds: int = Field(default=3600, ge=60)` — `ge=60` guards against runaway snapshot spam if misconfigured to 0
  * `retention_hourly: int = Field(default=24, ge=0)`
  * `retention_daily: int = Field(default=7, ge=0)`
  * `retention_weekly: int = Field(default=4, ge=0)`

  `V4Config` gains `backup: BackupConfig = Field(default_factory=BackupConfig)` (optional with default, backward-compatible — existing operator configs without a `[backup]` block continue to load and default to `enabled=True`). `load_config` gains `if "backup" in raw: payload["backup"] = raw["backup"]` mirroring the existing `if "storage" in raw:` shape at the end of `load_config`.

- [ ] **New module `packages/foreman/src/foreman/v4/pg_backup.py`** exposing:
  * `RetentionPolicy`: frozen dataclass with `hourly: int = 24`, `daily: int = 7`, `weekly: int = 4`.
  * `take_snapshot(dsn: str, dst_dir: Path, *, now: dt.datetime) -> Path` — creates `dst_dir` if it doesn't exist (`dst_dir.mkdir(parents=True, exist_ok=True)`), then runs:
    ```python
    result = subprocess.run(
        ["pg_dump", "--format=plain", "--clean", "--if-exists",
         "--no-owner", "--no-acl", dsn],
        capture_output=True, check=True,
    )
    ```
    Gzip-compresses `result.stdout` to `dst_dir / f"foreman-{now.strftime('%Y%m%dT%H%M%SZ')}.sql.gz"` and returns the path. Raises `subprocess.CalledProcessError` on non-zero exit; raises `OSError` on I/O failures. Does NOT swallow — the `BackupScheduler` swallows at the call site.
  * `prune_snapshots(dst_dir: Path, *, now: dt.datetime, retention: RetentionPolicy) -> list[Path]` — glob-matches `foreman-*.sql.gz` in `dst_dir`, parses timestamps from filenames (leaves unparseable filenames alone — operators may drop manual backups in this dir), and applies the three-tier algorithm:
    1. Files newer than `now - 24h`: keep most-recent `retention.hourly`; prune the rest.
    2. Files in `[now - 7d, now - 24h)`: bucket by UTC calendar day; keep most-recent per day; then keep at most `retention.daily` day-survivors (most-recent days win); prune the rest.
    3. Files in `[now - 28d, now - 7d)`: bucket by ISO 8601 week (Monday UTC); keep most-recent per week; then keep at most `retention.weekly` week-survivors; prune the rest.
    4. Files older than `now - 28d`: all pruned.
    Returns the list of deleted `Path` objects.
  * `BackupScheduler`: constructor `__init__(*, dsn: str, dst_dir: Path, interval_seconds: int, retention: RetentionPolicy, clock: Callable[[], dt.datetime], bus: EventBus)`. Method `tick() -> Path | None`: reads clock once, skips if `(now - _last_snapshot_at).total_seconds() < interval_seconds` (first call always fires since `_last_snapshot_at` initialises to `None`). On a snapshot tick:
    - Calls `take_snapshot(dsn=self._dsn, dst_dir=self._dst_dir, now=now)`. On `OSError` or `subprocess.SubprocessError`, swallows, publishes `BackupFailedEvent(at=now, phase="snapshot", reason=str(exc)[:500])` on `self._bus`, returns `None`.
    - Calls `prune_snapshots(dst_dir=self._dst_dir, now=now, retention=self._retention)`. On `OSError`, swallows, publishes `BackupFailedEvent(at=now, phase="prune", reason=str(exc)[:500])`, returns the snapshot path still (pruning failure does not cancel the snapshot).
    - On complete success publishes `BackupTakenEvent(at=now, path=str(snap_path), size_bytes=snap_path.stat().st_size, pruned_count=len(pruned))` and returns `snap_path`.
  * `_DisabledBackupScheduler`: no-op sentinel, stateless, `tick() -> Path | None` returns `None` immediately.
  * `BackupSchedulerLike = BackupScheduler | _DisabledBackupScheduler` type alias.
  * `BackupScheduler.from_config(config: BackupConfig, *, dsn: str, bus: EventBus, clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> BackupSchedulerLike` — classmethod factory: returns `_DisabledBackupScheduler()` when `config.enabled` is `False`; otherwise constructs and returns a real `BackupScheduler` from `config.dir`, `config.interval_seconds`, `dsn`, `bus`, `clock`, and `RetentionPolicy(hourly=config.retention_hourly, daily=config.retention_daily, weekly=config.retention_weekly)`.

- [ ] **`StructuredLogObserver`** in `packages/foreman/src/foreman/v4/observers/structured_log.py`:
  * Import `BackupTakenEvent` and `BackupFailedEvent` from `foreman.v4.events`.
  * Add two entries to `_EVENT_NAMES`: `BackupTakenEvent: ("backup_taken", logging.INFO)` and `BackupFailedEvent: ("backup_failed", logging.ERROR)`.
  * Inside the existing `isinstance(event, DaemonEvent):` block, add two sub-branches BEFORE the `self._log.log(...)` call:
    ```python
    if isinstance(event, BackupTakenEvent):
        daemon_payload["path"] = event.path
        daemon_payload["size_bytes"] = event.size_bytes
        daemon_payload["pruned_count"] = event.pruned_count
    elif isinstance(event, BackupFailedEvent):
        daemon_payload["phase"] = event.phase
        daemon_payload["reason"] = event.reason
    ```
  All other observers (`EventArchiveObserver`, `MetricsObserver`, `EventBus.publish` exception path) already have `isinstance(event, DaemonEvent): return` guards installed from issue #360 and require no changes.

- [ ] **`Daemon`** in `packages/foreman/src/foreman/v4/daemon.py`:
  * Add a module-level sentinel: `_DISABLED_BACKUP_SCHEDULER: BackupSchedulerLike = _DisabledBackupScheduler()`. Import `BackupSchedulerLike` and `_DisabledBackupScheduler` from `foreman.v4.pg_backup`. Update the neighbouring comment (line 37, currently references `_DISABLED_BACKUP_SCHEDULER` as if it exists above) to point at the actual constant.
  * `Daemon.__init__` gains keyword-only parameter `backup_scheduler: BackupSchedulerLike = _DISABLED_BACKUP_SCHEDULER` (bare module-level instance, not `dataclasses.field(default_factory=...)` — `Daemon` is a regular class with an explicit `__init__`). Store as `self._backup_scheduler = backup_scheduler`.
  * `Daemon.tick_once()` gains `self._backup_scheduler.tick()` (one unconditional call, no None-check) placed AFTER `self._pool.tick()` and BEFORE the bounded-drain `while` loop. The snapshot is a subprocess call (a few seconds at most); placing it after pool submission and before drain is symmetric with the existing ordering of side-effectful daemon work.

- [ ] **`bootstrap_cli_context`** in `packages/foreman/src/foreman/v4/bootstrap.py`:
  * Import `BackupScheduler` from `foreman.v4.pg_backup`.
  * Construct: `backup_scheduler = BackupScheduler.from_config(config.backup, dsn=config.storage.dsn, bus=bus)` (the `assert config.storage.dsn is not None` at line 60 already guarantees a non-None DSN).
  * Pass `backup_scheduler=backup_scheduler` to `Daemon(...)`.

- [ ] **New `foreman restore` CLI command** in `packages/foreman/src/foreman/v4/cli/restore.py`, registered in `cli/__init__.py` via `app.command("restore")(cmd_restore)`. Signature: `def cmd_restore(ctx: typer.Context, snapshot_file: Path = typer.Argument(...)) -> None`. Behavior in order:
  1. `config = ctx.obj.config if ctx.obj else None`; if `config is None`, print `"no config; ensure FOREMAN_V4_CONFIG is set"` to stderr and exit 1.
  2. `dsn = config.storage.dsn` (never None due to `StorageConfig` validator, but assert to narrow for mypy).
  3. Best-effort daemon-liveness check: if `PID_PATH.exists()`, read the PID, call `is_pid_alive(pid)`, and if alive print `"daemon is running (pid N); stop it first with \`foreman daemon stop\`"` to stderr and exit 1. Import `PID_PATH` and `is_pid_alive` from `foreman.v4.cli.daemon` (both are public names — `PID_PATH` at line 51, `is_pid_alive` at line 67 of `cli/daemon.py`). Same best-effort caveats as issue #360: inside a `docker compose run --rm daemon` one-off container the daemon's PID file lives in the daemon container's writable layer and is not visible; `docker compose stop daemon` is MANDATORY before invoking restore inside Docker.
  4. Validate `snapshot_file.exists() and snapshot_file.is_file()`; exit 1 with a clear message otherwise.
  5. `now = dt.datetime.now(dt.UTC)`. Take a pre-restore `pg_dump`: call `take_snapshot(dsn=dsn, dst_dir=Path(config.backup.dir), now=now)` then rename the result to `Path(config.backup.dir) / f"pre-restore-{now.strftime('%Y%m%dT%H%M%SZ')}.sql.gz"`. Print the pre-restore path. If the pre-restore dump fails (e.g., postgres is down), print the error and exit 1 — abort before touching anything.
  6. Decompress `snapshot_file` (`.gz` suffix) or copy as-is (plain `.sql`) to a `tempfile.NamedTemporaryFile(suffix=".sql", delete=False)`. Manage the tempfile path in a `try/finally` that calls `tmp_path.unlink(missing_ok=True)` to guarantee cleanup.
  7. Run: `subprocess.run(["psql", dsn, "--file", str(tmp_path), "--quiet"], check=True)`. On `subprocess.CalledProcessError`, print the error and exit 1.
  8. Print `"restored <snapshot_file>; pre-restore saved as <pre_restore_path>; start daemon with \`docker compose up -d daemon\`"` and exit 0.
  Import `take_snapshot` from `foreman.v4.pg_backup`.

- [ ] **`docker-compose.yml`**: Add `- ${HOME}/.foreman/backups:/foreman/backups` as a bind mount under the `daemon` service's `volumes:` block, with a comment `# foreman#434: pg_dump snapshot target — bind mount survives \`down -v\``. This is intentionally a bind mount, not a named volume: `docker compose down -v` wipes named volumes but leaves host-side bind-mounted directories untouched.

- [ ] **`docker/foreman/config.toml.template`**: Add a `[backup]` block placed between `[storage]` and `[apps]`:
  ```toml
  # foreman#434: pg_dump snapshot scheduler. Backups write to /foreman/backups
  # (bind-mounted to ${HOME}/.foreman/backups on the host — survives down -v).
  # Set enabled = false to disable; tune interval_seconds and retention_* to taste.
  [backup]
  enabled = true
  dir = "/foreman/backups"
  interval_seconds = 3600
  retention_hourly = 24
  retention_daily = 7
  retention_weekly = 4
  ```

- [ ] **`docs/RUNBOOK.md`** "Backups and restoration" section is UPDATED in place (the current content documents the DR gap; replace the gap notice with operational documentation):
  * What backups exist (`~/.foreman/backups/foreman-*.sql.gz`) and the schedule (default hourly, 24/7/4 retention, ~35 files total).
  * How to verify a backup is readable:
    ```bash
    gunzip -c ~/.foreman/backups/foreman-<ts>.sql.gz | head -5
    # Should print PostgreSQL header comments (-- PostgreSQL database dump ...)
    ```
  * Restore procedure with a PROMINENT WARNING block (must appear BEFORE the restore commands, not as a footnote) that `docker compose stop daemon` is MANDATORY and that the postgres sidecar must remain running:
    ```
    # MANDATORY: stop the daemon FIRST. The postgres sidecar must stay running.
    # The foreman restore PID-file check is best-effort inside Docker — a one-off
    # `docker compose run` container cannot see the daemon's PID file (it lives in
    # the daemon's writable layer, not on a shared volume). Running restore against
    # a live daemon can corrupt either the live DB or the restored state.
    docker compose stop daemon

    # One-off container mounts the same volumes/binds as the daemon:
    docker compose run --rm daemon \
        foreman restore /foreman/backups/foreman-<ts>.sql.gz
    docker compose up -d daemon
    ```
  * One-step undo: the pre-restore dump is saved as `pre-restore-<ts>.sql.gz` alongside the backups. Run `foreman restore` against it to undo.
  * Tuning: how to change `interval_seconds`, `retention_*`, or `enabled = false` in the `[backup]` TOML block.
  * The "What survives what" table at the end of the RUNBOOK gains a `~/.foreman/backups` host-dir row: this directory survives all `docker compose` operations because it lives on the host filesystem, not in a Docker volume.

- [ ] **New test file `packages/foreman/tests/v4/test_pg_backup.py`** covering, at minimum:
  * `test_take_snapshot_writes_gz_and_returns_path`: monkeypatch `subprocess.run` to return a `CompletedProcess` with `stdout=b"-- test dump"` and `returncode=0`; call `take_snapshot(dsn="postgresql://...", dst_dir=tmp_path, now=fixed_dt)`; assert the returned path exists, filename matches `^foreman-\d{8}T\d{6}Z\.sql\.gz$`, and the path can be gunzip-decompressed to the expected bytes.
  * `test_take_snapshot_propagates_pg_dump_failure`: monkeypatch `subprocess.run` to raise `subprocess.CalledProcessError(1, "pg_dump")`; assert the exception propagates and no `.sql.gz` file is written.
  * `test_take_snapshot_creates_dst_dir_if_missing`: call with a `dst_dir = tmp_path / "new" / "sub"`; assert the directory is created and the snapshot written.
  * `test_prune_keeps_hourly_then_daily_then_weekly`: seed `tmp_path` with files named `foreman-<ts>.sql.gz` using `os.utime` and timestamp-encoded filenames (60 spaced 1 hour apart over the last 60 hours, plus 30 spaced 1 day apart over the last 30 days); call `prune_snapshots(..., retention=RetentionPolicy(24, 7, 4))`; assert exactly 35 survivors, and the survivors are the most-recent per tier.
  * `test_prune_leaves_unparseable_filenames_alone`: seed with one valid snapshot and a file `manual-backup.sql.gz`; assert `manual-backup.sql.gz` still exists after pruning.
  * `test_scheduler_tick_respects_interval`: stub clock to `t0`; construct `BackupScheduler(interval_seconds=3600, ...)`; first `tick()` returns a path (snapshot taken); advance clock 30 min, `tick()` returns `None` (too soon); advance 31 more min, `tick()` returns a path (second snapshot).
  * `test_scheduler_disabled_via_from_config_returns_none`: `BackupScheduler.from_config(BackupConfig(enabled=False), dsn="...", bus=EventBus())`; assert returned object is `_DisabledBackupScheduler`; `tick()` returns `None` and writes nothing.
  * `test_scheduler_swallows_snapshot_error_and_publishes_failed_event`: inject a `EventBus` with a captor subscriber; monkeypatch `foreman.v4.pg_backup.take_snapshot` to raise `OSError("disk full")`; call `tick()`; assert returns `None` (no crash) AND a `BackupFailedEvent(phase="snapshot")` was published on the bus.

- [ ] **New test file `packages/foreman/tests/v4/cli/test_restore.py`** covering, at minimum:
  * `test_restore_calls_psql_with_correct_args`: create a `tmp_path / "foreman-20260627T120000Z.sql"` file; monkeypatch `foreman.v4.pg_backup.take_snapshot` (the pre-restore call) to write a dummy file; monkeypatch `subprocess.run` to capture the `psql` call; build a `CliContext` with a mock config (`config.storage.dsn = "postgresql://..."`, `config.backup.dir = str(tmp_path)`); invoke via `CliRunner().invoke(app, ["restore", str(snapshot)], obj=ctx)`; assert exit code 0 and `subprocess.run` was called with `["psql", "postgresql://...", "--file", ..., "--quiet"]`.
  * `test_restore_refuses_when_daemon_alive`: write a PID file pointing at `os.getpid()` (alive); monkeypatch `foreman.v4.cli.restore.PID_PATH` to that file; invoke `cmd_restore`; assert exit code 1 and `subprocess.run` NOT called.
  * `test_restore_refuses_missing_snapshot`: invoke with a path that does not exist; assert exit code 1.

- [ ] **`packages/foreman/tests/v4/test_config.py`** extended with:
  * `test_backup_block_defaulted_when_absent`: TOML without `[backup]`; assert `config.backup.enabled is True` and `config.backup.interval_seconds == 3600`.
  * `test_backup_block_interval_too_small`: TOML with `[backup]` setting `interval_seconds = 30`; assert `ValidationError` (`ge=60` fires).
  * `test_backup_block_extras_forbidden`: TOML with `[backup]` carrying an unknown key; assert `ValidationError`.

- [ ] **`packages/foreman/tests/v4/test_daemon.py`** extended with:
  * `test_tick_once_calls_backup_scheduler`: inject `MagicMock()` as `backup_scheduler`; call `daemon.tick_once()`; assert `backup_scheduler.tick()` called exactly once.
  * `test_tick_once_uses_disabled_scheduler_by_default`: construct `Daemon(...)` without `backup_scheduler=`; call `tick_once()`; assert no exception raised.

## Approach

**Pattern naming (Decision 4).** No GoF pattern fits cleanly — this is "periodic job on the existing tick loop." Two Google engineering principles apply:

1. **SRP.** `take_snapshot` knows only how to shell out to `pg_dump` and gzip the output. `prune_snapshots` knows only the three-tier retention algorithm. `BackupScheduler` knows only when to invoke the other two. `Daemon.tick_once()` knows nothing about what a snapshot is — it calls `self._backup_scheduler.tick()` unconditionally. Each unit is tested in isolation.

2. **"Make the right thing easy".** One `[backup]` TOML block, one `foreman restore` command, one `~/.foreman/backups/` directory, one RUNBOOK section. An operator recovering from `down -v` has exactly one cluster of docs to read. The disabled-scheduler sentinel eliminates conditional wiring in `tick_once()` and keeps test fixtures that don't pass `backup_scheduler=` green without any change.

**Why `pg_dump` (logical) over `pg_basebackup` (physical/WAL).** The issue body explicitly identifies `pg_dump` as the right first cut: same retention/restore ergonomics as the SQLite scheduler, simpler tooling, and plain-format SQL dumps are human-readable and restoreable with a single `psql` invocation. `pg_basebackup` + WAL archiving is full PITR — correct for a multi-operator production service, overkill for single-operator dogfood at the current scale.

**Why `--format=plain` (SQL) over `--format=custom` (binary).** Plain format is restoreable with `psql` directly — no separate `pg_restore` tool, operator-readable with `gunzip | head`, fewer moving parts. The custom format requires `pg_restore` and a different restore invocation. Plain format is the lowest-friction path for this first DR story.

**Why `--clean --if-exists --no-owner --no-acl`.** `--clean` generates `DROP TABLE IF EXISTS` before `CREATE TABLE`, making the restore idempotent when piped into `psql`. `--if-exists` prevents errors when an object doesn't exist at restore time (e.g., an interrupted prior restore). `--no-owner` and `--no-acl` drop `ALTER TABLE ... OWNER TO` and GRANT/REVOKE statements that can fail if the restore user lacks superuser privileges — the `foreman` user created all objects and reconnects as itself, so ownership lines are redundant overhead.

**Why installing `postgresql-client-16` via the PGDG apt repo.** `python:3.12-slim` is based on Debian Bookworm; the default `postgresql-client` package on Bookworm is version 15. `pg_dump` 15 cannot dump from a PostgreSQL 16 server (`"server version: 16.x; pg_dump version: 15.x"` error at runtime). The PGDG apt repository provides `postgresql-client-16`, matching the `postgres:16-alpine` sidecar exactly. This is a one-time Dockerfile change.

**Why a host bind mount (not a named volume).** `docker compose down -v` is the primary failure scenario the issue calls out. Named volumes are wiped by `-v`; host bind-mounted directories are not — Docker owns only the mount, not the host directory. The `~/.foreman/` directory is already a foreman state surface (holds `~/.foreman/keys/*.pem` via Compose secrets); adding `~/.foreman/backups/` is consistent with that convention.

**Why the pre-restore snapshot takes a live `pg_dump` before restoring.** The most dangerous restore failure is "operator restores the wrong file." Without a pre-restore dump, the live database at that moment is irretrievable. The `pg_dump` pre-restore + `pre-restore-<ts>.sql.gz` rename makes the entire restore operation reversible via the same `foreman restore` command.

## Sub-requests (topologically sorted)

1. **Add `postgresql-client-16` to `Dockerfile`** — new `RUN` step after the system-deps block: add PostgreSQL PGDG apt repo, then `apt-get install -y postgresql-client-16`. Provides `pg_dump` and `psql` at the correct major version.

2. **Restore `BackupTakenEvent` and `BackupFailedEvent` in `packages/foreman/src/foreman/v4/events.py`** — add them as `DaemonEvent` subclasses per the acceptance criteria. The `DaemonEvent` base (lines 68–79) is already in place.

3. **Add `BackupConfig` to `packages/foreman/src/foreman/v4/config.py`** — pydantic model, `backup` field on `V4Config`, extension of `load_config`.

4. **Create `packages/foreman/src/foreman/v4/pg_backup.py`** — `RetentionPolicy`, `take_snapshot`, `prune_snapshots`, `BackupScheduler`, `_DisabledBackupScheduler`, `BackupSchedulerLike`, `BackupScheduler.from_config`.

5. **Update `StructuredLogObserver` in `packages/foreman/src/foreman/v4/observers/structured_log.py`** — two new `_EVENT_NAMES` entries, two `isinstance` sub-branches inside the existing `DaemonEvent` block, two new imports.

6. **Extend `Daemon` in `packages/foreman/src/foreman/v4/daemon.py`** — module-level sentinel constant, `backup_scheduler` kwarg on `__init__`, `self._backup_scheduler.tick()` call in `tick_once()`.

7. **Wire the scheduler in `packages/foreman/src/foreman/v4/bootstrap.py`** — `BackupScheduler.from_config(config.backup, dsn=config.storage.dsn, bus=bus)` passed to `Daemon(...)`.

8. **Create `packages/foreman/src/foreman/v4/cli/restore.py`** with `cmd_restore`; register in `packages/foreman/src/foreman/v4/cli/__init__.py`.

9. **Infrastructure files** — bind mount in `docker-compose.yml`, `[backup]` block in `docker/foreman/config.toml.template`.

10. **Write all tests** — `test_pg_backup.py`, `test_restore.py`, extensions to `test_config.py` and `test_daemon.py`.

11. **Update `docs/RUNBOOK.md`** "Backups and restoration" section.

## File-level changes

| File | Change |
|---|---|
| `Dockerfile` | Add `RUN` step: PostgreSQL PGDG apt repo + `postgresql-client-16` |
| `packages/foreman/src/foreman/v4/events.py` | Add `BackupTakenEvent(DaemonEvent)` + `BackupFailedEvent(DaemonEvent)` |
| `packages/foreman/src/foreman/v4/config.py` | Add `BackupConfig` model; add `backup` field on `V4Config`; extend `load_config` |
| `packages/foreman/src/foreman/v4/pg_backup.py` | NEW: `RetentionPolicy`, `take_snapshot`, `prune_snapshots`, `BackupScheduler`, `_DisabledBackupScheduler`, `BackupSchedulerLike`, `from_config` |
| `packages/foreman/src/foreman/v4/observers/structured_log.py` | Add two `_EVENT_NAMES` entries; add two `isinstance` sub-branches inside the `DaemonEvent` block; import `BackupTakenEvent`, `BackupFailedEvent` |
| `packages/foreman/src/foreman/v4/daemon.py` | Add `_DISABLED_BACKUP_SCHEDULER` module-level sentinel; add `backup_scheduler` kwarg to `__init__`; add `self._backup_scheduler.tick()` call in `tick_once()` after `self._pool.tick()` and before the bounded-drain loop |
| `packages/foreman/src/foreman/v4/bootstrap.py` | Construct `BackupScheduler.from_config(...)` and pass to `Daemon(...)` |
| `packages/foreman/src/foreman/v4/cli/restore.py` | NEW: `cmd_restore` |
| `packages/foreman/src/foreman/v4/cli/__init__.py` | `app.command("restore")(cmd_restore)` registration + import |
| `docker-compose.yml` | Add `${HOME}/.foreman/backups:/foreman/backups` bind mount on `daemon` service |
| `docker/foreman/config.toml.template` | Add `[backup]` block between `[storage]` and `[apps]` |
| `packages/foreman/tests/v4/test_pg_backup.py` | NEW: 8 tests (snapshot writes gz, propagates failure, creates dir, prune tiers, prune leaves unparseable, scheduler interval, scheduler disabled, scheduler swallows error) |
| `packages/foreman/tests/v4/cli/test_restore.py` | NEW: 3 tests (calls psql, refuses when daemon alive, refuses missing snapshot) |
| `packages/foreman/tests/v4/test_config.py` | Append 3 backup-block tests (defaulted, interval too small, extras forbidden) |
| `packages/foreman/tests/v4/test_daemon.py` | Append 2 backup-scheduler tests (scheduler tick called, default sentinel no-op) |
| `docs/RUNBOOK.md` | Update "Backups and restoration" section; update "What survives what" table |

## Alternatives considered

1. **`pg_basebackup` + WAL archiving for point-in-time recovery.** Physical backup with full PITR capability. Rejected: requires WAL archiving configuration (`archive_command`) on the Postgres sidecar, a dedicated WAL-storage volume, and a more complex restore procedure (`pg_restore` into a recovery-mode standby). Overkill for single-operator dogfood where a weekly `pg_dump` gives the same practical guarantee and restores in one command.

2. **Sidecar `foreman-backup` container using the `postgres:16-alpine` image** (which ships `pg_dump 16` natively, no Dockerfile change required). Rejected: adds a third container to manage and monitor, a second log surface, and a second failure mode for what is structurally one responsibility. The `CloneRefresher` pattern already demonstrates that the daemon tick is the right home for periodic daemon-level jobs.

3. **`docker exec foreman-postgres pg_dump ...` from inside the daemon container** — avoids installing `postgresql-client` in the daemon image by delegating to the sidecar. Rejected: requires mounting `/var/run/docker.sock` in the daemon container — the same high-privilege Docker socket Watchtower uses, which Foreman deliberately does not need. Trading `postgresql-client-16` for a Docker socket mount is a bad security trade-off.

4. **Host cron / systemd timer / Task Scheduler running `pg_dump` directly on the host.** Rejected: per-operator OS-level config (three different formats for Linux/macOS/Windows), not in version control, fails silently when the operator's cron environment lacks the correct `FOREMAN_PG_DSN`. The daemon-tick scheduler is version-controlled and reads the same TOML config every operator tests against.

## Open questions

None. The issue body is explicit: `pg_dump` to `~/.foreman/backups/` with tiered retention and a `foreman restore` CLI counterpart. Implementation-time judgment calls (exact `pg_dump` flags, prune algorithm tie-breaking) are resolved in the Approach and Acceptance sections above.

## Out of scope

- **Off-host backup** (S3, B2, rsync). Local bind-mount catches `down -v` and volume corruption; off-host backup is a separate epic that can use these `.sql.gz` files as its source.
- **WAL archiving / point-in-time recovery.** See Alternatives.
- **Backing up `foreman-repos`, `foreman-state`, `foreman-logs`, `foreman-claude-sessions` volumes.** Project clones are reconstructable from GitHub; state/log volumes hold config and structured logs that are either reproducible or observability-only; Claude session transcripts are managed separately by the crash-recovery work.
- **Encrypting snapshots at rest.** `~/.foreman/backups/` inherits the host filesystem's permissions (operator-only on a personal machine). Encryption belongs in the off-host-backup epic.
- **Schema-migration of restored snapshots.** Restoring a dump taken at schema vN into a daemon at schema vN+1 is handled at the application layer — `PostgresTicketRepository.from_dsn()` applies the current schema DDL. Non-additive schema changes are out of scope; the RUNBOOK notes the same-version recommendation.
- **Backing up `~/.foreman/keys/*.pem`.** Operator holds canonical copies; GitHub holds the public half. DR for key loss is key rotation, not backup.
- **A `foreman backup` manual-trigger command.** The issue asks only for a scheduled backup (via the daemon tick) and a restore command. A manual trigger can follow in a separate ticket.
