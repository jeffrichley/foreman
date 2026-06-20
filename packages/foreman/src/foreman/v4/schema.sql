-- packages/foreman/src/foreman/v4/schema.sql
--
-- Foreman v4 SQLite schema. Two tables:
--   tickets         — the ticket row, with operator-hold columns
--   state_instances — the journal; one row per (state, entry) tuple
--
-- See docs/superpowers/specs/2026-06-13-foreman-v4-substrate-redesign-design.md
-- "Durability + resume" for the column semantics.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT    NOT NULL,
    issue_number    INTEGER NOT NULL,
    current_state   TEXT    NOT NULL,
    held_by         TEXT,
    held_at         TEXT,
    held_reason     TEXT,
    depends_on      TEXT    NOT NULL DEFAULT '[]',
    next_action_at  TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE(project, issue_number)
);

CREATE TABLE IF NOT EXISTS state_instances (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id               INTEGER NOT NULL REFERENCES tickets(id),
    state_name              TEXT    NOT NULL,
    sequence                INTEGER NOT NULL,
    entered_at              TEXT    NOT NULL,
    execute_started_at      TEXT,
    execute_completed_at    TEXT,
    exited_at               TEXT,
    outcome_kind            TEXT,
    outcome_payload         TEXT,
    next_state              TEXT,
    failure_phase           TEXT,
    failure_reason          TEXT,
    UNIQUE(ticket_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_state_instances_inflight
    ON state_instances(ticket_id)
    WHERE exited_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_tickets_held
    ON tickets(held_by)
    WHERE held_by IS NOT NULL;

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       INTEGER NOT NULL,
    instance_id     INTEGER NOT NULL,
    event_type      TEXT    NOT NULL,
    state_name      TEXT    NOT NULL,
    sequence        INTEGER NOT NULL,
    at              TEXT    NOT NULL,
    payload         TEXT    NOT NULL  -- JSON-encoded extra fields
);

CREATE INDEX IF NOT EXISTS idx_events_ticket
    ON events(ticket_id, at);
