# Postgres Migration Runbook (v5)

## Overview

Foreman v5 replaces the SQLite data store with PostgreSQL 16. The change closes
two persistent bug classes:

- **`database is locked` errors** (foreman#404) — SQLite's writer-serialization
  breaks under concurrent role dispatches; asyncpg's connection pool eliminates
  the contention entirely.
- **Drop-resurrection** (foreman#403) — a SQLite WAL checkpoint race could
  resurface completed tickets; Postgres's MVCC semantics make this impossible.

The ticket repository sits behind the `TicketRepository` Protocol; the rest of
the daemon, CLI, and role runners are unchanged.

---

## Status: cutover complete, SQLite removed

This cutover is **done**. SQLite has been fully removed from foreman — there
is no longer a `sqlite` engine option, no default-to-SQLite behavior, and no
silent fallback. The storage selector is **Postgres-only and loud-fails** if it
is not configured (e.g. `FOREMAN_STORAGE_ENGINE` unset or not `postgres`); it
will not boot against a missing or non-Postgres config.

`docker/foreman/config.toml.template` now **ships a `[storage]` Postgres
block**, and `FOREMAN_PG_DSN` is substituted into it at container start:

```toml
[storage]
engine = "postgres"
dsn = "${FOREMAN_PG_DSN}"
pool_min = 2
pool_max = 10
```

`envsubst` expands `${FOREMAN_PG_DSN}` at container start using the value set
in `docker-compose.yml`:

```yaml
FOREMAN_PG_DSN: postgresql://foreman:${FOREMAN_PG_PASSWORD}@postgres:5432/foreman
```

The remainder of this runbook records how the one-time cutover was performed and
how to operate the Postgres stack. It is no longer a set of pending follow-up
steps.

---

## How the config reaches the daemon

The actual config mechanism (not hand-editing a file):

1. `docker/foreman/config.toml.template` is baked into the image at
   `/etc/foreman/config.toml.template` during `docker build`.
2. At container start, `docker/entrypoint.sh` runs:
   ```bash
   envsubst < /etc/foreman/config.toml.template > /foreman/state/config.toml
   ```
   This expands `${FOREMAN_*}` placeholders using environment variables
   injected by Docker Compose at runtime.
3. The daemon reads `/foreman/state/config.toml` (the rendered output, on the
   `foreman-state` named volume).

Operators control config by setting env vars in `.env` — not by editing
`config.toml` on the host. The template is the source of truth.

---

## `[storage]` block reference

```toml
[storage]
engine = "postgres"
dsn = "postgresql://foreman:<password>@postgres:5432/foreman"
pool_min = 2
pool_max = 10
```

`engine = "postgres"` (string literal) selects `PostgresTicketRepository`.
`dsn` is required when engine is `postgres`; Pydantic enforces this at startup.
`pool_min` / `pool_max` size the asyncpg connection pool; defaults are 2 and 10.

Within the container, the hostname `postgres` resolves via Docker's internal DNS
to the `foreman-postgres` sidecar defined in `docker-compose.yml`. The DSN is
assembled from `FOREMAN_PG_PASSWORD` (set in `.env`) automatically:

```
FOREMAN_PG_DSN: postgresql://foreman:${FOREMAN_PG_PASSWORD}@postgres:5432/foreman
```

---

## Migration history (how the one-time cutover was performed)

> These steps are **historical** — they record how the v4→v5 cutover was run
> once. SQLite is now fully removed; there is no `sqlite` engine to migrate from
> and no migrate tool to re-run. New deployments start directly on Postgres
> (see "Bring-up sequence" below).

### Cold-start (the path that was used)

Used when in-flight tickets could drain before the switch or were few enough to
re-enqueue manually.

1. **Drain the queue.** Run `foreman ps` and confirm no tickets are in an
   active role-dispatch state (Planning, Reviewing, Implementing, Fixing,
   Merging). Hold or defer any that cannot drain:
   ```
   foreman hold <ticket-id>
   ```

2. **Stop the daemon.**
   ```
   docker compose stop daemon
   ```

3. **Set `FOREMAN_PG_PASSWORD` in `.env`.**
   ```
   FOREMAN_PG_PASSWORD=<choose-a-strong-password>
   ```
   (The `[storage]` Postgres block now ships in the config template, so no
   hand-edit of the template is required.)

4. **Bring up Postgres and wait for it to be healthy.**
   ```
   docker compose up -d postgres
   docker compose ps        # wait until postgres shows "healthy"
   ```
   The `pg_isready` healthcheck polls every 5 seconds with up to 10 retries
   (50 seconds total). The daemon's `depends_on: condition: service_healthy`
   ensures it will not start until Postgres passes.

5. **Bring up the daemon.**
   ```
   docker compose up -d daemon
   ```

6. **Verify** (see Verification section below).

### Hot-port of existing state (historical — tool removed)

For deployments with long-tenured tickets, a one-time `foreman migrate-v4-to-v5`
tool ported tickets + state instances (current state, sequence, timing,
outcomes, dependencies) from the old SQLite file into Postgres. The events
archive was intentionally not ported, and state-instance integer IDs were
reassigned as fresh BIGSERIAL IDs (sequence ordering within a ticket was
preserved, which is what the state machine relies on).

**That migrate tool has been removed along with SQLite** — there is no longer a
SQLite file to port from, so this path no longer exists.

---

## Bring-up sequence

```bash
# 1. Ensure FOREMAN_PG_PASSWORD is set in .env, then:
docker compose up -d postgres

# 2. Confirm postgres is healthy before proceeding:
docker compose ps
# "foreman-postgres" should show "(healthy)"

# 3. Start the daemon (will block on the healthcheck automatically):
docker compose up -d daemon

# 4. Tail logs to confirm clean startup:
docker compose logs -f daemon
```

Expected log line on successful v5 start:

```json
{"event": "container_start", "image_sha": "...", "allow_dirty": false, "foreman_v4_config": "/foreman/state/config.toml", ...}
```

---

## Verification

```bash
foreman ps
```

If this returns (including an empty queue) without an error, the daemon is up
and the repository is reachable.

To confirm the bugs are gone:

- **`database is locked`** — run two concurrent `foreman enqueue` calls; both
  should complete without a SQLite locking error in the daemon logs.
- **Drop-resurrection** — drop a ticket and confirm it does not reappear in
  `foreman ps` after the next poller tick.

---

## Rollback

There is **no rollback to SQLite.** SQLite has been removed from foreman
entirely — there is no `sqlite` engine to set, no `db_path` config field, and
no SQLite file left on the `foreman-state` volume. The storage selector is
Postgres-only and loud-fails if Postgres is not configured, so reverting the
`[storage]` block does not fall back to SQLite; it fails to boot.

The only recovery path now is forward: fix the Postgres configuration /
connectivity and bring the daemon back up against the sidecar. Postgres data
survives on the `foreman-pg-data` named volume across `docker compose stop` and
`docker compose down`; only `docker compose down -v` destroys it. (Postgres DR
backups are tracked separately as
[foreman#434](https://github.com/jeffrichley/foreman/issues/434) and not yet
built.)

---

## Windows caveat

`${HOME}` in `docker-compose.yml` is used for two bind mounts:

```yaml
- ${HOME}/.foreman/backups:/foreman/backups
- ${HOME}/.foreman:/root/.foreman
```

`${HOME}` resolves correctly in WSL2 and Git Bash Docker contexts. It does
**not** expand in native Windows `cmd.exe`. If you run `docker compose` from
a Windows terminal directly, override the paths in a local
`docker-compose.override.yml`:

```yaml
services:
  daemon:
    volumes:
      - C:/Users/<you>/.foreman/backups:/foreman/backups
      - C:/Users/<you>/.foreman:/root/.foreman
```

This override is gitignored by convention and should not be committed.
