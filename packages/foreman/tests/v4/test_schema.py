"""The v4 SQLite schema — tickets + state_instances tables.

This tests that the SQL file applies cleanly and the tables have the
columns the spec mandates. Repository-level CRUD is tested separately.
"""
import sqlite3
from pathlib import Path

import pytest

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "foreman"
    / "v4"
    / "schema.sql"
)


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def test_schema_file_exists():
    assert SCHEMA_PATH.exists(), f"schema file missing at {SCHEMA_PATH}"


def test_tickets_table_columns(db: sqlite3.Connection):
    cols = {row[1] for row in db.execute("PRAGMA table_info(tickets)")}
    assert {
        "id",
        "project",
        "issue_number",
        "current_state",
        "created_at",
        "updated_at",
        "held_by",
        "held_at",
        "held_reason",
    } <= cols


def test_state_instances_table_columns(db: sqlite3.Connection):
    cols = {row[1] for row in db.execute("PRAGMA table_info(state_instances)")}
    assert {
        "id",
        "ticket_id",
        "state_name",
        "sequence",
        "entered_at",
        "execute_started_at",
        "execute_completed_at",
        "exited_at",
        "outcome_kind",
        "outcome_payload",
        "next_state",
        "failure_phase",
        "failure_reason",
    } <= cols


def test_state_instances_unique_ticket_sequence(db: sqlite3.Connection):
    db.execute(
        "INSERT INTO tickets(project, issue_number, current_state, created_at, updated_at) "
        "VALUES ('p', 1, 'Queued', '2026-06-13T00:00:00', '2026-06-13T00:00:00')"
    )
    db.execute(
        "INSERT INTO state_instances(ticket_id, state_name, sequence, entered_at) "
        "VALUES (1, 'Queued', 1, '2026-06-13T00:00:00')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO state_instances(ticket_id, state_name, sequence, entered_at) "
            "VALUES (1, 'Queued', 1, '2026-06-13T00:00:01')"
        )


def test_in_flight_query(db: sqlite3.Connection):
    db.execute(
        "INSERT INTO tickets(project, issue_number, current_state, created_at, updated_at) "
        "VALUES ('p', 1, 'Planning', '2026-06-13T00:00:00', '2026-06-13T00:00:00')"
    )
    db.execute(
        "INSERT INTO state_instances(ticket_id, state_name, sequence, entered_at, exited_at) "
        "VALUES (1, 'Queued', 1, '2026-06-13T00:00:00', '2026-06-13T00:00:01')"
    )
    db.execute(
        "INSERT INTO state_instances(ticket_id, state_name, sequence, entered_at) "
        "VALUES (1, 'Planning', 2, '2026-06-13T00:00:02')"
    )
    rows = db.execute(
        "SELECT state_name FROM state_instances WHERE exited_at IS NULL"
    ).fetchall()
    assert rows == [("Planning",)]
