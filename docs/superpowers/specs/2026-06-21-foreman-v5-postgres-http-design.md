# Spec: Foreman v5 — PostgreSQL substrate + HTTP control plane

## Goal

Replace foreman v4's embedded SQLite store with a PostgreSQL database running as a sibling container, and add an HTTP control plane to the daemon process so external clients (the dashboard, future CLI-over-HTTP, scripts) can query state and submit mutations without sharing the daemon's filesystem.

Preserve the v4 state machine, observer fan-out, role pipeline, and operator CLI surface verbatim. v5 is a substrate migration plus an additive HTTP layer — not a behavior change.

**Motivation.** Two operational failure classes drove this work:

- **`database is locked` under daemon write contention** — operator mutations (`foreman hold/resume/reset/drop`) raise `sqlite3.OperationalError: database is locked` whenever they coincide with a daemon write transaction. The operator's recovery path today is a race-the-lock retry loop. Tracked in foreman#404. SQLite's WAL mode + `busy_timeout` would soften it; PostgreSQL's MVCC dissolves it.
- **`foreman drop` doesn't stick** — `foreman drop <id>` reports success but the Poller resurrects the ticket on its next tick. The proximate cause is that the `Failed` transition the drop writes is not durably observed by the Poller's next-tick read — a SQLite-shape interaction with the daemon's in-process cache. Tracked in foreman#403. PostgreSQL's read-after-write semantics across connections eliminate the foot-gun.

Both bugs are SQLite-shape symptoms, not v4 logic bugs. The v4 substrate redesign assumed SQLite was a stable enough foundation for a single-writer + occasional-operator-reader topology; today's incidents (2026-06-21) showed it isn't, even with one daemon and one operator. As soon as a second consumer (the dashboard) joins, the contention surface expands.

**Why now.** The foreman-dashboard project (separate spec, same day) needs an external client to read state and submit mutations. Three options were on the table:

1. Read SQLite directly from the dashboard process. Rejected — pickled fields, schema drift, no transactional safety, fragile across container boundaries.
2. Add an HTTP control plane while keeping SQLite. Rejected — SQLite contention scales sub-linearly with reader count, and the dashboard is a third writer (mutations) on top of daemon + operator CLI.
3. Migrate to PostgreSQL + add HTTP control plane in the same release. Selected — addresses both contention bug classes, gives the dashboard a clean network seam, and the migration cost is bounded because v4's persistence is already behind the `TicketRepository` Protocol.

**Scope of this decision.** Establish the v5 substrate. The subsequent implementation plan (via writing-plans) decomposes into bite-sized tasks against this spec. The foreman-dashboard spec (sibling document) consumes the HTTP API defined here.

**Out of scope.**

- Multi-daemon / multi-host topology. v5 is still single-daemon-per-deployment. Postgres makes future horizontal scaling possible; v5 does not exercise it.
- Authentication on the HTTP control plane beyond local-bind + shared-secret. Real auth (OAuth, mTLS, etc.) is a v6 concern. v5's threat model is "trusted localhost + tailnet" (operator decision per project).
- Rewriting v4 state semantics. The state machine, the five-hook lifecycle, the role pipeline, the two-phase PR pattern — all preserved exactly.
- Migrating historical SQLite ticket data to Postgres. v5 ships with a one-shot migration script (best-effort, idempotent), but cold-starting with an empty Postgres is the supported default; in-flight tickets are drained on v4 before the v5 cutover. The migration script is documented in the impl PR; not a runtime dependency.

## Acceptance criteria

- **PostgreSQL is the single source of truth** for ticket state, state instances, events, and daemon health. The `TicketRepository` Protocol from v4 is preserved; a new `PostgresTicketRepository` implementation replaces `SqliteTicketRepository` and passes the existing repository contract test suite verbatim. The in-memory implementation continues to back the unit tests.
- **No `sqlite3` import in the v5 runtime path.** The SQLite-backed `SqliteTicketRepository` is retained only as a reference for the migration script and as a fallback in `tests/`; production code paths import only the Postgres impl.
- **HTTP control plane runs in the daemon process.** FastAPI app bound to `127.0.0.1:8765` by default (configurable). Exposed endpoints (locked, expanded below): `GET /projects`, `GET /projects/{name}`, `GET /projects/{name}/events` (SSE stream), `POST /tickets/enqueue`, `POST /tickets/{id}/hold`, `POST /tickets/{id}/resume`, `POST /tickets/{id}/reset`, `GET /health`.
- **Operator CLI continues to work without HTTP.** `foreman ps/show/log/hold/resume/...` connect to Postgres directly using the same `PostgresTicketRepository` the daemon uses. CLI doesn't depend on the HTTP plane being up — the HTTP layer is for *external* clients (dashboard) and observability tooling.
- **Three-container topology.** `docker-compose.yml` (or operator runbook equivalent) defines:
  1. `foreman-daemon` — runs the autonomous-loop coordinator + HTTP control plane.
  2. `foreman-dashboard` — separate service, talks HTTP to `foreman-daemon`. Spec'd in the sibling dashboard design doc.
  3. `postgres` — Postgres 16+ container, named volume for data.
  Today's single-container `foreman-daemon` deployment is supported during transition via a `[storage].engine = "sqlite"` config knob, but this is a deprecated path with a sunset date.
- **Alembic migrations** for the v5 schema. The initial migration creates `tickets`, `state_instances`, `events`, and `daemon_health` tables. The v5 schema diverges from v4 in three ways (typed JSON columns instead of TEXT-of-JSON, native UUID for instance IDs, indexed event_type for queryability) — the migration captures the deltas.
- **`PRAGMA busy_timeout` parity bug eliminated.** PostgreSQL's MVCC means concurrent reads + writes don't block each other; operator mutations don't race against daemon ticks. foreman#404 closes on this spec.
- **`foreman drop` resurrection bug eliminated.** With Postgres, each transaction sees committed state, so the Poller's next tick reads the `Failed` transition the drop wrote. foreman#403 closes on this spec **plus** the Poller-side defense from the drop spec (skip resurrection when latest state is terminal).
- **Daemon health table** records each daemon-process tick with a heartbeat row + last-error-per-project. This is the substrate the dashboard's "substrate-hot watch" surface (deferred to v1+) consumes. Adding the table now means the future surface is a SELECT, not a schema migration.
- **Crash-only resume preserved.** Daemon restart reads last state from Postgres and resumes — same semantics as v4, different store.
- **Parity test suite.** The full v4 e2e test (`tests/v4/test_e2e_lifecycle.py` and friends) runs against `PostgresTicketRepository` in CI via a Postgres testcontainer. No v4 behavior regression is acceptable.
- **HTTP API contract test.** Each endpoint has a contract test that exercises request shape, response shape, and error cases. Tests run against the real FastAPI app via `httpx.AsyncClient` (not against a mock).
- **Tagging / version handshake.** Daemon advertises `foreman-version: v5` in HTTP health responses. Dashboard refuses to start against a daemon that doesn't speak v5.

## Approach

### Substrate swap (Postgres replaces SQLite)

The `TicketRepository` Protocol is the only seam v4 exposes between the state machine and persistence. v5 implements `PostgresTicketRepository` against the same Protocol — drop-in.

Architecture choices for the Postgres layer:

- **`asyncpg` over `psycopg`.** The daemon already has an async event loop for the role dispatcher and the HTTP server; `asyncpg` reuses it without a thread pool. The repository contract has a sync surface (v4 wrote it that way to match SQLite's stdlib), so v5 either (a) makes the Protocol async, or (b) wraps async-pg in a sync-over-async helper using `asyncio.run_coroutine_threadsafe` against the daemon's loop. Recommend **(a)** — `async def` everywhere — and update v4's callers. The v4 tests already use `pytest-asyncio` in places; the change is mechanical.
- **Connection pool**, not per-call connect. `asyncpg.create_pool(min_size=2, max_size=10)` at daemon bootstrap. CLI processes use a smaller pool (`min_size=1, max_size=3`) because they're short-lived.
- **No ORM.** Repository code writes parameterized SQL directly. Same shape as the SQLite impl. Rationale: foreman's persistence is ~6 tables with bounded queries; SQLAlchemy adds a layer that buys nothing here and complicates async + connection-pool reasoning.
- **Schema changes from v4** (locked deltas):
  - `tickets.depends_on` becomes `JSONB` (was `TEXT` of JSON-encoded list).
  - `state_instances.outcome_payload` becomes `JSONB`.
  - `state_instances.id` becomes `UUID` (was autoincrement `INTEGER`). Rationale: UUIDs are urlsafe in HTTP paths.
  - `events.payload` becomes `JSONB`; `events.event_type` gets a GIN index for substring queries (foreman state inspection often queries event type prefixes).
  - All ISO-8601-string datetime columns become native `TIMESTAMPTZ`.
- **The migration path: cold start, not data port.** v4 deployments drain in-flight tickets (`foreman ps` empty), stop the daemon, switch the config to `[storage].engine = "postgres"` + connection URL, start the v5 daemon against an empty Postgres. The migration script is offered as a best-effort import for users who can't drain (e.g. long-tenured `NeedsHelp` tickets they want to retain); it's lossy on event-log history by design (event archive becomes too large to port faithfully).

### HTTP control plane (additive layer)

A FastAPI app is mounted on the daemon's `asyncio` event loop alongside the Poller, QueueManager, and Worker Pool. It exposes the API documented above and below.

Architectural shape:

- **Endpoint module structure**: `packages/foreman/src/foreman/v5/http/` houses the FastAPI app. One file per resource: `projects.py`, `tickets.py`, `health.py`. Pydantic request/response models live alongside.
- **Read endpoints use the repository directly.** No service-layer indirection — the repository contract is rich enough to serve every read query the API needs. Endpoints are 5-15 line functions: parse path/query → call repo → serialize.
- **Mutation endpoints reuse the CLI mutation handlers.** The v4 `cli/mutations.py` already has `cmd_hold/cmd_resume/cmd_reset/cmd_drop` functions that take a repository + ticket id + reason; HTTP endpoints call the same functions, sharing all the state-validation logic. The CLI wraps the same handler with `typer.Option` plumbing; the HTTP layer wraps with FastAPI request parsing. **Single source of truth for "what hold means."**
- **SSE for project-detail event streams.** `GET /projects/{name}/events` returns `text/event-stream`. The implementation subscribes a new in-process observer to the existing EventBus, filters events to the requested project, and streams them to the SSE response. When the client disconnects, the observer unsubscribes. No polling, no missed events between connect and first read.
- **Authentication is bind-address + shared secret.** Default bind is `127.0.0.1:8765` — localhost-only. Operators who want tailnet access set `[http].bind = "0.0.0.0:8765"` and `[http].shared_secret = "<value>"`. Every mutation requires `Authorization: Bearer <secret>` when a secret is configured. Reads behind `127.0.0.1` are unauthenticated by default; if `[http].auth_reads_too = true`, the shared secret gates reads as well.
- **No CSRF, no cookies, no sessions.** HTTP API is a programmatic contract between daemon and dashboard/CLI. The dashboard renders HTML server-side; it's not a SPA exposing the API to a browser, so CSRF doesn't apply.

### Operator CLI integration

The CLI talks to Postgres directly, not over HTTP. Rationale: the CLI runs in the same container as the daemon (or on the operator's host with creds), so localhost-Postgres latency is sub-millisecond. Going through HTTP adds a serialization round-trip without buying anything.

There's no separate "CLI-talks-to-HTTP" mode in v5. If a future operator persona needs that (cross-host CLI), it'd be a new mode behind an env flag; not in v5 scope.

### Three-container topology

`docker-compose.yml` shape:

```yaml
services:
  postgres:
    image: postgres:16-alpine
    volumes: [foreman-pg-data:/var/lib/postgresql/data]
    environment:
      POSTGRES_USER: foreman
      POSTGRES_PASSWORD: <from-env>
      POSTGRES_DB: foreman
    healthcheck: pg_isready
  foreman-daemon:
    image: ghcr.io/jeffrichley/foreman:v5
    depends_on: { postgres: { condition: service_healthy } }
    environment:
      FOREMAN_DB_URL: postgres://foreman:<from-env>@postgres:5432/foreman
    volumes:
      - ~/.foreman:/root/.foreman
      - <each project worktree mount>
    ports: ["8765:8765"]  # control plane
  foreman-dashboard:
    image: ghcr.io/jeffrichley/foreman-dashboard:v0
    depends_on: { foreman-daemon: { condition: service_healthy } }
    environment:
      FOREMAN_API_URL: http://foreman-daemon:8765
    ports: ["8000:8000"]  # dashboard UI
```

Local-dev variant (no compose) is supported: operator runs Postgres via `docker run` (or natively-installed), points `FOREMAN_DB_URL` at it, and runs `foreman daemon start` against an installed wheel.

### Daemon health surface (foundation for the substrate-hot-loop watch)

A new `daemon_health` table records:

```sql
CREATE TABLE daemon_health (
    id              BIGSERIAL PRIMARY KEY,
    daemon_id       TEXT NOT NULL,              -- e.g. "foreman-daemon-<host>-<pid>"
    project         TEXT,                       -- NULL = daemon-level, non-NULL = per-project
    event_type      TEXT NOT NULL,              -- "started", "stopped", "tick", "error", "config_reload"
    at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    detail          JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_daemon_health_recent ON daemon_health (at DESC);
CREATE INDEX idx_daemon_health_project ON daemon_health (project, at DESC) WHERE project IS NOT NULL;
```

The Poller writes a `"tick"` row at each tick start (with `detail.tick_seconds`, projects polled, queue depth). Errors write an `"error"` row with stack trace + project. Daemon startup writes `"started"`, SIGTERM-handler writes `"stopped"`.

The dashboard's deferred substrate-hot watch surface is a query against this table: "how many `started` rows in the last 24h per daemon_id" + "latest `error` row per project." Adding the table now (even if v0 of the dashboard doesn't read it) ensures the data is collected from day one — by the time the dashboard surfaces it, there's a useful history. Pepper flagged this gap explicitly during the dashboard brainstorm; the v5 spec resolves it.

Retention: a daily cron (in the daemon, not external) prunes `daemon_health` rows older than 90 days. Tunable.

### Topological order of the rewrite

1. **Postgres schema** + alembic baseline migration (`packages/foreman/src/foreman/v5/schema/`).
2. **`PostgresTicketRepository`** — implements the same Protocol as `SqliteTicketRepository`; runs the existing contract test suite verbatim against a testcontainer-backed Postgres.
3. **Async sweep of v4 callers** — convert state-machine, observer, Poller, and CLI repository calls from sync to async. Mechanical change; the v4 codebase already uses `asyncio` for the role dispatch path.
4. **`DaemonHealthRecorder`** — observer that writes `daemon_health` rows on EventBus events. Wires into the existing observer fan-out.
5. **HTTP control plane** — FastAPI app, endpoints, SSE stream. Reuses `cli/mutations.py` handlers.
6. **Configuration** — `V5Config` extends `V4Config` with `[storage]` (engine, db_url, pool sizes) and `[http]` (bind, shared_secret, auth_reads_too) sections.
7. **Three-container compose** — docker-compose.yml + operator runbook for cold start.
8. **Parity test suite** — full v4 e2e tests pointed at Postgres testcontainer in CI.
9. **One-shot migration script** — `foreman migrate-v4-to-v5 --sqlite-path <p> --postgres-url <u>`. Documented in the impl PR; not part of normal v5 install.
10. **Documentation** — operator README updates, v4 → v5 migration guide.

## HTTP API contract (locked)

### `GET /projects`

Returns the list of projects currently configured in `~/.foreman/v5/config.toml`, each with a status snapshot.

**Response shape (JSON):**

```json
{
  "projects": [
    {
      "name": "foreman",
      "repo": "jeffrichley/foreman",
      "status": "healthy",
      "ticket_counts": {
        "queued": 0,
        "in_flight": 2,
        "needs_help": 0,
        "done_last_24h": 4
      },
      "last_activity_at": "2026-06-21T23:14:02Z"
    }
  ],
  "summary": {
    "total_projects": 3,
    "by_status": {"stalled": 0, "needs_you": 0, "healthy": 2, "idle": 1}
  }
}
```

`status` is derived using the precedence-ordered rules from the dashboard spec (stalled → needs-you → idle → healthy). The rules live in `packages/foreman/src/foreman/v5/status.py`; both this endpoint and the dashboard call into them.

### `GET /projects/{name}`

Returns a single-project detail snapshot.

**Response shape (JSON):**

```json
{
  "name": "foreman",
  "repo": "jeffrichley/foreman",
  "status": "healthy",
  "tickets": [
    {
      "id": 21,
      "issue_number": 405,
      "title": "...",
      "current_state": "Implementing",
      "in_state_since": "2026-06-21T22:55:00Z",
      "current_role_progress": {"tick": 4, "total": 6},
      "held": false,
      "url": "https://github.com/jeffrichley/foreman/issues/405"
    }
  ],
  "metrics": {
    "prs_merged_last_24h": 2,
    "fixer_retries_last_24h": 1,
    "lead_time_p50_minutes": 47,
    "stuck_role_count": 0
  },
  "role_health": [
    {"role": "planner", "last_run_at": "...", "last_outcome": "success"},
    {"role": "reviewer", "last_run_at": "...", "last_outcome": "success"},
    ...
  ]
}
```

The metrics block is aggregated from `state_instances` + `events`. The aggregation lives in `packages/foreman/src/foreman/v5/metrics.py` so the same numbers are available to the dashboard and to CLI commands (`foreman metrics --project <name>`).

### `GET /projects/{name}/events` (SSE)

`text/event-stream` response. Each event has `event:` + `data:` lines per SSE spec. Event types:

- `state_transition` — fired on every state instance transition for the project. `data` is the JSON-encoded `TransitionEvent`.
- `role_progress` — fired when a long-running role emits a progress hint (tick N of M). `data` is `{"ticket_id": ..., "role": "planner", "tick": 4, "total": 6}`.
- `outcome` — fired on every role outcome. `data` is the `Outcome` JSON.
- `heartbeat` — fired every 15s to keep the connection alive. `data` is `{"at": "..."}`.

Implementation: the endpoint subscribes a transient observer to the EventBus filtered by project. When the client disconnects, the observer unsubscribes.

### `POST /tickets/enqueue`

Body: `{"project": "<name>", "issue_number": <int>}`.

Response: `{"ticket_id": <int>, "state": "Queued"}` or `{"error": "..."}`.

Behavior: identical to today's `foreman enqueue` CLI — bypasses the Poller's label-watching, directly creates a `Queued` ticket. Used by the dashboard's `+ Plan` button when the operator explicitly requests a ticket be queued.

### `POST /tickets/{id}/hold`

Body: `{"reason": "<string>"}`.

Behavior: identical to `foreman hold` CLI. Sets `held_by`, `held_at`, `held_reason`. Daemon's next tick observes the hold and stops dispatching.

### `POST /tickets/{id}/resume`

No body.

Behavior: clears the hold.

### `POST /tickets/{id}/reset`

Body: `{"reason": "<string>", "no_retrigger": <bool>}`.

Behavior: identical to `foreman reset` — wipes labels, branches, worktrees, SQLite row (here Postgres row). With `no_retrigger=true` (recommended default for the dashboard), the `foreman:plan` label is NOT reapplied; with `false`, the label is reapplied so the Poller picks the ticket up on the next tick.

### `GET /health`

Liveness probe. Returns `{"status": "ok", "foreman_version": "v5", "uptime_seconds": <int>, "db_pool_size": <int>, "last_tick_at": "<iso>"}`.

Used by docker-compose healthcheck + dashboard's daemon-version handshake.

## Sub-requests

1. **Create `packages/foreman/src/foreman/v5/` package skeleton.** `__init__.py`, `repository.py` (re-exports), `storage/postgres.py` (impl shell), `http/__init__.py`, `http/app.py`, `http/projects.py`, `http/tickets.py`, `http/health.py`, `schema/` (alembic dir), `config.py`, `metrics.py`, `status.py`. Empty modules with imports; no logic yet.
2. **Alembic baseline migration.** Initial revision creates `tickets`, `state_instances`, `events`, `daemon_health` tables with the v5 schema (JSONB columns, UUID state-instance IDs, TIMESTAMPTZ, GIN indexes). Run-time: `alembic upgrade head`.
3. **Postgres testcontainer fixture.** `tests/v5/conftest.py` spins up a `postgres:16-alpine` testcontainer per test session, runs alembic migrations, yields a connection URL. Tests get a clean schema per-test via `BEGIN ... ROLLBACK` per-test.
4. **`PostgresTicketRepository`** — async `asyncpg`-backed implementation of the `TicketRepository` Protocol. Same method signatures, same return types, same exceptions (`TicketNotFoundError`, etc.). Adapted contract test suite from v4 runs against it green.
5. **Async sweep of v4 callers.** `TicketRepository` Protocol methods become `async def`. State-machine `transition()` becomes `async`. Observers stay sync (they accept Event objects; the bus emits sync). Poller becomes `async`. CLI commands become `async`. ThreadPoolExecutor wrapping the role dispatcher is unchanged. Subprocess-driven role dispatch is unchanged. This is mostly mechanical — the v4 codebase already has substantial async; the sync-Repository was the holdout.
6. **`DaemonHealthRecorder`** observer. Subscribes to EventBus. Writes `daemon_health` rows on daemon start/stop, every tick, every error. Tested via the existing observer test pattern.
7. **`V5Config`.** Extends `V4Config` with `[storage]` section (engine: "postgres" | "sqlite" deprecated, db_url, pool_min, pool_max) and `[http]` section (bind, shared_secret, auth_reads_too, request_log_path). TOML loader with the same validation pattern as v4.
8. **FastAPI app.** `http/app.py` builds the app with the configured shared_secret middleware and CORS (off by default — dashboard runs same-host). Routers from `projects.py`, `tickets.py`, `health.py` are mounted under `/`.
9. **Endpoints.** Implement each endpoint per the contract above. Reuse `cli/mutations.py` handlers for mutations. Add Pydantic request/response models for every endpoint. Add contract tests via `httpx.AsyncClient` for every endpoint.
10. **SSE stream.** `GET /projects/{name}/events` implementation: subscribe a transient observer to the EventBus, filter to project, serialize each event to SSE format, write to response stream until client disconnect. Use `sse-starlette` (small, async-native) for the SSE plumbing. Test with an async client.
11. **Mount HTTP server in daemon process.** Daemon's bootstrap starts uvicorn programmatically (not via the CLI) on the configured bind. Bind failures are fatal. SIGTERM cleanly stops the server alongside the Poller and WorkerPool.
12. **Status computation.** `packages/foreman/src/foreman/v5/status.py` implements the precedence rules from the dashboard spec (stalled → needs-you → idle → healthy). Pure function over a ticket-count snapshot. Tested with table-driven cases per rule.
13. **Metrics aggregation.** `packages/foreman/src/foreman/v5/metrics.py` implements the metric tile values consumed by `GET /projects/{name}` and the future `foreman metrics` CLI command. SQL queries pre-defined; no ORM. Tested with seeded Postgres rows.
14. **docker-compose.yml + operator runbook.** Three-service compose (postgres + foreman-daemon + foreman-dashboard). Operator runbook documents cold start, in-flight drain procedure, and the one-shot migration script.
15. **One-shot migration script.** `foreman migrate-v4-to-v5 --sqlite-path <p> --postgres-url <u>`. Reads v4 SQLite, writes v5 Postgres. Idempotent (skips tickets that already exist by `(project, issue_number)`). Lossy on event-log history by design (events table not migrated).
16. **Parity test suite.** Adapt every v4 e2e test (especially `tests/v4/test_e2e_lifecycle.py`) to run against the Postgres testcontainer. Add v5-specific tests for HTTP API contract + SSE behavior.
17. **Just check + final PR.** Full `just check` green. Single PR titled `feat(v5): postgres substrate + http control plane`. Adversarial-review pass before merge per the standing rule.

## Migration & deployment notes

### Cold-start path (recommended)

1. Stop the v4 daemon (`foreman daemon stop`).
2. Drain in-flight tickets: confirm `foreman ps` shows no Queued/Planning/SpecReview/SpecFix/Implementing/ImplReview/ImplFix/Merging tickets. Hold-and-defer any that can wait.
3. Bring up the v5 stack: `docker-compose up -d postgres` first, wait for healthy, then `docker-compose up -d foreman-daemon foreman-dashboard`.
4. The daemon's bootstrap runs `alembic upgrade head` against the new Postgres before the Poller starts.
5. Re-apply `foreman:plan` to issues that were held; the Poller picks them up.

### Hot-port path (migration script, opt-in)

For deployments that can't drain (long-tenured `NeedsHelp` tickets the operator wants to retain):

1. Stop the v4 daemon.
2. `foreman migrate-v4-to-v5 --sqlite-path ~/.foreman/state.db --postgres-url postgres://...` runs the migration script.
3. Start the v5 stack.

The migration script ports `tickets` and `state_instances`. It does NOT port `events` (too large, low value) or `daemon_health` (v5-only). State-instance IDs are remapped from INTEGER to new UUIDs; the mapping is logged.

### Sunset of the SQLite engine

`[storage].engine = "sqlite"` is supported in v5.0.0 as a transitional knob for single-container deployments that can't run Postgres. It is **deprecated on day 1** and removed in v5.1.0. The deprecation timeline lives in the v5 README's "supported configurations" section.

## Adversarial review (self)

Before transition to writing-plans, this spec gets the standing adversarial-review pass. Open questions to surface for Jeff's review:

1. **Async-everywhere is a large mechanical churn.** Roughly every repository call across v4 grows an `await`. Risk: a test or two slip through with sync-call shape and silently break. Mitigation: a one-time grep for `repository\.[a-z_]+\(` in non-test code after the sweep, plus mypy with `--strict` enabled on the v5 package.
2. **Postgres-in-tests is slower than SQLite-in-tests.** v4 tests start in <2s; testcontainer Postgres adds ~5s per session for image pull + startup. Acceptable on CI (already runs Docker), local-dev mitigation is a session-scoped fixture (already proposed) + reusable Postgres data dir across test runs.
3. **The HTTP API surface is wider than today's CLI surface.** SSE, JSON shapes, content-type contracts — all new code. Mitigation: contract tests per endpoint, plus the dashboard spec consumes this surface and any mismatch surfaces immediately during dashboard development.
4. **The 90-day `daemon_health` retention may be too short or too long.** Operators with low-traffic projects may want a year; operators with high-frequency runs may want 30 days. Acceptable risk: tune via a config knob in v5.0.0; defaults are documented; no schema change needed to revisit.
5. **No `Authorization` header on reads by default.** If an operator binds `0.0.0.0` without setting `auth_reads_too = true`, status data leaks on the tailnet. Mitigation: bootstrap refuses to start if `bind ≠ 127.0.0.1` AND `shared_secret` is unset. Loud error, not silent permissiveness.

## Acceptance criteria checklist

- [ ] `PostgresTicketRepository` passes the existing v4 repository contract test suite verbatim.
- [ ] No `sqlite3` import in `packages/foreman/src/foreman/v5/` production code.
- [ ] FastAPI app exposes all 8 endpoints (`GET /projects`, `GET /projects/{name}`, `GET /projects/{name}/events`, `POST /tickets/enqueue`, `POST /tickets/{id}/hold`, `POST /tickets/{id}/resume`, `POST /tickets/{id}/reset`, `GET /health`) with contract tests.
- [ ] SSE endpoint streams events for the requested project and unsubscribes on client disconnect.
- [ ] `foreman hold/resume/reset/drop` mutations succeed under concurrent daemon ticks — no more `database is locked` (foreman#404 closes).
- [ ] `foreman drop` followed by 10 Poller ticks shows the ticket remains terminal — no resurrection (foreman#403 closes, with the Poller-side defense per the drop spec).
- [ ] `daemon_health` table is populated on daemon start, tick, error, stop. Query against it returns rows.
- [ ] `docker-compose up` brings up postgres + foreman-daemon + foreman-dashboard. Dashboard's `GET /health` returns `foreman_version: v5`.
- [ ] Alembic migration creates the v5 schema. `alembic downgrade base` cleanly removes it.
- [ ] Full v4 e2e test suite runs against the Postgres testcontainer and passes.
- [ ] `just check` clean.
- [ ] Adversarial-review PR pass before merge (standing rule).

## Out of scope

- Multi-daemon coordination, leader election, horizontal scaling.
- OAuth or mTLS on the HTTP API.
- A web UI for the API beyond the dashboard (which is a separate service).
- Cross-host CLI (`foreman ps` on a different host from the daemon).
- Live migration of an in-flight ticket from v4 SQLite to v5 Postgres (the script is for drained-state archive only).
- Replacing the role dispatch subprocess pattern.
- Anything dashboard-specific — that lives in the sibling dashboard spec.

## References

- foreman v4 substrate redesign (2026-06-13): `docs/superpowers/specs/2026-06-13-foreman-v4-substrate-redesign-design.md` — the v4 spec this builds on.
- foreman dashboard design (2026-06-21, sibling spec): `docs/superpowers/specs/2026-06-21-foreman-dashboard-design.md` — consumes the HTTP API defined here.
- foreman#403 — `foreman drop` doesn't delete SQLite row; resurrects on next Poller tick. Closes on this spec.
- foreman#404 — operator mutations fail with `database is locked` under daemon write contention. Closes on this spec.
- Pepper's substrate-hot-loop watch flag (2026-06-21 dashboard brainstorm): the `daemon_health` table is the data foundation; the dashboard surface comes later.
