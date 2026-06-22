-- packages/foreman/src/foreman/v4/postgres_schema.sql
--
-- Foreman v5 PostgreSQL schema. Behavioral parity with schema.sql
-- (the SQLite v4 schema). Integer PKs preserved — the TicketRepository
-- Protocol types ids as int across the codebase; UUID would break the
-- drop-in property for no benefit (/tickets/21 is already urlsafe).
--
-- Type deltas from SQLite:
--   INTEGER PK AUTOINCREMENT -> BIGSERIAL
--   TEXT (datetime, ISO-8601) -> TIMESTAMPTZ
--   TEXT (json)               -> JSONB
--   TEXT (depends_on json)    -> JSONB (default '[]')

CREATE TABLE IF NOT EXISTS tickets (
    id              BIGSERIAL   PRIMARY KEY,
    project         TEXT        NOT NULL,
    issue_number    INTEGER     NOT NULL,
    current_state   TEXT        NOT NULL,
    held_by         TEXT,
    held_at         TIMESTAMPTZ,
    held_reason     TEXT,
    depends_on      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    next_action_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (project, issue_number)
);

CREATE TABLE IF NOT EXISTS state_instances (
    id                      BIGSERIAL   PRIMARY KEY,
    ticket_id               BIGINT      NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    state_name              TEXT        NOT NULL,
    sequence                INTEGER     NOT NULL,
    entered_at              TIMESTAMPTZ NOT NULL,
    execute_started_at      TIMESTAMPTZ,
    execute_completed_at    TIMESTAMPTZ,
    exited_at               TIMESTAMPTZ,
    outcome_kind            TEXT,
    outcome_payload         JSONB,
    next_state              TEXT,
    failure_phase           TEXT,
    failure_reason          TEXT,
    UNIQUE (ticket_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_state_instances_inflight
    ON state_instances (ticket_id)
    WHERE exited_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_tickets_held
    ON tickets (held_by)
    WHERE held_by IS NOT NULL;

CREATE TABLE IF NOT EXISTS events (
    id              BIGSERIAL   PRIMARY KEY,
    ticket_id       BIGINT      NOT NULL,
    instance_id     BIGINT      NOT NULL,
    event_type      TEXT        NOT NULL,
    state_name      TEXT        NOT NULL,
    sequence        INTEGER     NOT NULL,
    at              TIMESTAMPTZ NOT NULL,
    payload         JSONB       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ticket
    ON events (ticket_id, at);
