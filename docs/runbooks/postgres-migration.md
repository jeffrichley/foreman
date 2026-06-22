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

## CRITICAL: Config template gap

`docker/foreman/config.toml.template` does **not** currently contain a
`[storage]` section. The `FOREMAN_PG_DSN` env var is passed into the daemon
container (see `docker-compose.yml`), but `envsubst` only substitutes
placeholders that exist in the template. Until a `[storage]` block is added to
the template, the daemon loads with `engine = "sqlite"` (the default) and
ignores `FOREMAN_PG_DSN` entirely.

**Add the following block to `docker/foreman/config.toml.template`** (after the
`[backup]` section, before `[apps]`):

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

This is a **required step before v5 is operational**. Without it, the daemon
silently falls back to SQLite and the bugs above are not fixed. Consider this a
follow-up task before the v5 deploy.

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

## Migration paths

### Cold-start (recommended)

Use this when in-flight tickets can drain before the switch or are few enough to
re-enqueue manually.

1. **Drain the queue.** Run `foreman ps` and confirm no tickets are in an
   active role-dispatch state (Planning, Reviewing, Implementing, Fixing,
   Merging). Hold or defer any that cannot drain:
   ```
   foreman hold <ticket-id>
   ```

2. **Stop the v4 daemon.**
   ```
   docker compose stop daemon
   ```

3. **Add `[storage]` to the config template** (see the critical section above).
   Rebuild the image so the updated template is baked in:
   ```
   docker compose build daemon
   ```

4. **Set `FOREMAN_PG_PASSWORD` in `.env`.**
   ```
   FOREMAN_PG_PASSWORD=<choose-a-strong-password>
   ```

5. **Bring up Postgres and wait for it to be healthy.**
   ```
   docker compose up -d postgres
   docker compose ps        # wait until postgres shows "healthy"
   ```
   The `pg_isready` healthcheck polls every 5 seconds with up to 10 retries
   (50 seconds total). The daemon's `depends_on: condition: service_healthy`
   ensures it will not start until Postgres passes.

6. **Bring up the daemon.**
   ```
   docker compose up -d daemon
   ```

7. **Verify** (see Verification section below).

### Hot-port (opt-in, lossy on event history)

Use this when the deployment has long-tenured tickets whose state history must
be preserved in the new store. The migration command is idempotent on
`(project, issue_number)`, so it is safe to run multiple times.

```
foreman migrate-v4-to-v5 \
    --sqlite-path /path/to/foreman.sqlite \
    --postgres-url "postgresql://foreman:<password>@<host>:5432/foreman"
```

**What is ported:** tickets + state instances (current state, sequence, timing,
outcomes, dependencies).

**What is NOT ported:** the events archive. The `events` table holds the full
history of `EventBus` emissions; it is large, low value for day-to-day
operation, and not ported by design. State-instance integer IDs are not
preserved — Postgres assigns fresh BIGSERIAL IDs; sequence ordering within a
ticket is preserved, which is what the state machine relies on.

After the port succeeds, update the `[storage]` section in the config template
and bring up the v5 stack as in the cold-start path above (steps 3–7).

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

1. Stop the daemon:
   ```
   docker compose stop daemon
   ```

2. Remove the `[storage]` block from `docker/foreman/config.toml.template` (or
   set `engine = "sqlite"`). Rebuild:
   ```
   docker compose build daemon
   ```

3. Restart the daemon:
   ```
   docker compose up -d daemon
   ```

The SQLite file at `[daemon].db_path` (`/foreman/state/foreman.sqlite` inside
the container, on the `foreman-state` named volume) is untouched by the
Postgres path. The v4 ticket history is fully intact.

Postgres data survives on the `foreman-pg-data` named volume. Only
`docker compose down -v` destroys it.

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
