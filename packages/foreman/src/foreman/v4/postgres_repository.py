"""PostgresTicketRepository — production persistence on PostgreSQL.

Behavioral parity with SqliteTicketRepository — the same
RepositoryContract test suite runs against both. Synchronous, matching
v4's ThreadPoolExecutor-based daemon. A psycopg_pool.ConnectionPool
serves the worker threads; Postgres MVCC removes the write contention
the SQLite impl serialized with an RLock, so there is no lock here.

Integer PKs (BIGSERIAL) preserved — the Protocol types ids as int.

Datetime contract: callers pass naive dt.datetime via ``now=`` (the
contract's ``_now()`` and the literal assertions like
``dt.datetime(2026, 6, 13, 13)`` are all naive). SqliteTicketRepository
stores ``value.isoformat()`` and reads it back via
``dt.datetime.fromisoformat`` — so a naive input round-trips to the
identical naive datetime, and equality holds. Postgres TIMESTAMPTZ
normalizes to UTC and psycopg returns tz-aware datetimes on read, which
would FAIL ``==`` against the contract's naive literals. To preserve
exact parity we normalize naive→UTC on write (``_to_db``) and strip the
tzinfo back off on read (``_from_db``), yielding the same naive value
the caller passed. See _to_db / _from_db below.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from foreman.v4.outcome import OutcomeKind
from foreman.v4.records import StateInstanceRecord, TicketRecord
from foreman.v4.repository import (
    StateInstanceNotFoundError,
    TicketAlreadyExistsError,
    TicketNotFoundError,
)

_SCHEMA = Path(__file__).with_name("postgres_schema.sql")


def _to_db(value: dt.datetime | None) -> dt.datetime | None:
    """Normalize a caller datetime for TIMESTAMPTZ storage.

    Naive datetimes are assumed UTC, so round-tripping is stable and
    matches the SqliteTicketRepository contract behavior.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


def _from_db(value: dt.datetime | None) -> dt.datetime | None:
    """Strip the tzinfo psycopg adds to TIMESTAMPTZ reads.

    The contract asserts equality against naive datetimes (the values
    callers passed in). Postgres returns tz-aware UTC datetimes; convert
    to UTC and drop the tzinfo so the round-trip yields the identical
    naive value SqliteTicketRepository would have returned.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(dt.UTC).replace(tzinfo=None)
    return value


def _ticket_from_row(row: dict[str, Any]) -> TicketRecord:
    return TicketRecord(
        id=row["id"],
        project=row["project"],
        issue_number=row["issue_number"],
        current_state=row["current_state"],
        created_at=_from_db(row["created_at"]),
        updated_at=_from_db(row["updated_at"]),
        held_by=row["held_by"],
        held_at=_from_db(row["held_at"]),
        held_reason=row["held_reason"],
        depends_on=list(row["depends_on"]),
        next_action_at=_from_db(row["next_action_at"]),
    )


def _instance_from_row(row: dict[str, Any]) -> StateInstanceRecord:
    kind = OutcomeKind(row["outcome_kind"]) if row["outcome_kind"] else None
    return StateInstanceRecord(
        id=row["id"],
        ticket_id=row["ticket_id"],
        state_name=row["state_name"],
        sequence=row["sequence"],
        entered_at=_from_db(row["entered_at"]),
        execute_started_at=_from_db(row["execute_started_at"]),
        execute_completed_at=_from_db(row["execute_completed_at"]),
        exited_at=_from_db(row["exited_at"]),
        outcome_kind=kind,
        outcome_payload=row["outcome_payload"],
        next_state=row["next_state"],
        failure_phase=row["failure_phase"],
        failure_reason=row["failure_reason"],
    )


_TERMINAL_STATES = ("Done", "Failed")


class PostgresTicketRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        with self._pool.connection() as conn:
            conn.execute(_SCHEMA.read_text(encoding="utf-8"))
            conn.commit()

    @classmethod
    def from_dsn(
        cls, dsn: str, *, pool_min: int = 2, pool_max: int = 10
    ) -> PostgresTicketRepository:
        pool = ConnectionPool(
            dsn,
            min_size=pool_min,
            max_size=pool_max,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
        return cls(pool)

    def close(self) -> None:
        self._pool.close()

    # --- Ticket CRUD ---

    def create_ticket(self, *, project: str, issue_number: int, now: dt.datetime) -> TicketRecord:
        ts = _to_db(now)
        with self._pool.connection() as conn:
            try:
                row = conn.execute(
                    """
                    INSERT INTO tickets
                        (project, issue_number, current_state,
                         created_at, updated_at, depends_on)
                    VALUES (%s, %s, 'Queued', %s, %s, '[]'::jsonb)
                    RETURNING *
                    """,
                    (project, issue_number, ts, ts),
                ).fetchone()
            except psycopg.errors.UniqueViolation as exc:
                conn.rollback()
                raise TicketAlreadyExistsError(f"{project}#{issue_number}") from exc
            conn.commit()
            assert row is not None
            return _ticket_from_row(row)

    def get_ticket(self, ticket_id: int) -> TicketRecord:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id = %s", (ticket_id,)).fetchone()
        if row is None:
            raise TicketNotFoundError(str(ticket_id))
        return _ticket_from_row(row)

    def get_ticket_by_issue(self, *, project: str, issue_number: int) -> TicketRecord:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM tickets WHERE project = %s AND issue_number = %s",
                (project, issue_number),
            ).fetchone()
        if row is None:
            raise TicketNotFoundError(f"{project}#{issue_number}")
        return _ticket_from_row(row)

    def list_open_tickets(self) -> list[TicketRecord]:
        with self._pool.connection() as conn:
            # psycopg v3 adapts a Python list to a Postgres array; ``<>
            # ALL(%s)`` is the array-aware spelling of ``NOT IN (...)``.
            # ``NOT IN %s`` with a tuple does NOT work — psycopg renders
            # the tuple as a single ``$1`` placeholder, which Postgres
            # rejects as a syntax error.
            rows = conn.execute(
                "SELECT * FROM tickets WHERE current_state <> ALL(%s) ORDER BY id",
                (list(_TERMINAL_STATES),),
            ).fetchall()
        return [_ticket_from_row(r) for r in rows]

    def list_all_tickets(self) -> list[TicketRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()
        return [_ticket_from_row(r) for r in rows]

    def set_ticket_state(self, ticket_id: int, new_state: str, *, now: dt.datetime) -> None:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE tickets SET current_state = %s, updated_at = %s WHERE id = %s",
                (new_state, _to_db(now), ticket_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            conn.commit()

    def hold_ticket(self, ticket_id: int, *, held_by: str, reason: str, now: dt.datetime) -> None:
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                UPDATE tickets
                   SET held_by = %s, held_at = %s, held_reason = %s,
                       updated_at = %s
                 WHERE id = %s
                """,
                (held_by, _to_db(now), reason, _to_db(now), ticket_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            conn.commit()

    def resume_ticket(self, ticket_id: int, *, now: dt.datetime) -> None:
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                UPDATE tickets
                   SET held_by = NULL, held_at = NULL, held_reason = NULL,
                       updated_at = %s
                 WHERE id = %s
                """,
                (_to_db(now), ticket_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            conn.commit()

    def delete_ticket(self, ticket_id: int) -> None:
        # ON DELETE CASCADE on state_instances.ticket_id drops the journal
        # rows automatically (matches InMemory's manual cascade).
        with self._pool.connection() as conn:
            cur = conn.execute("DELETE FROM tickets WHERE id = %s", (ticket_id,))
            if cur.rowcount == 0:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            conn.commit()

    def set_next_action_at(self, ticket_id: int, *, when: dt.datetime) -> None:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE tickets SET next_action_at = %s, updated_at = %s WHERE id = %s",
                (_to_db(when), _to_db(when), ticket_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            conn.commit()

    def clear_next_action_at(self, ticket_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE tickets SET next_action_at = NULL WHERE id = %s",
                (ticket_id,),
            )
            conn.commit()

    # --- State-instance journal ---

    def open_state_instance(
        self, *, ticket_id: int, state_name: str, sequence: int, now: dt.datetime
    ) -> StateInstanceRecord:
        with self._pool.connection() as conn:
            # FK enforces ticket existence; surface a clean error first.
            exists = conn.execute("SELECT 1 FROM tickets WHERE id = %s", (ticket_id,)).fetchone()
            if exists is None:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            row = conn.execute(
                """
                INSERT INTO state_instances
                    (ticket_id, state_name, sequence, entered_at)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (ticket_id, state_name, sequence, _to_db(now)),
            ).fetchone()
            conn.commit()
            assert row is not None
            return _instance_from_row(row)

    def get_state_instance(self, instance_id: int) -> StateInstanceRecord:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM state_instances WHERE id = %s", (instance_id,)
            ).fetchone()
        if row is None:
            raise StateInstanceNotFoundError(str(instance_id))
        return _instance_from_row(row)

    def mark_execute_started(self, instance_id: int, *, now: dt.datetime) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE state_instances SET execute_started_at = %s WHERE id = %s",
                (_to_db(now), instance_id),
            )
            conn.commit()

    def mark_execute_completed(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        outcome_kind: OutcomeKind,
        outcome_payload: dict[str, Any],
        next_state: str,
    ) -> None:
        from psycopg.types.json import Jsonb

        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE state_instances
                   SET execute_completed_at = %s, outcome_kind = %s,
                       outcome_payload = %s, next_state = %s
                 WHERE id = %s
                """,
                (
                    _to_db(now),
                    outcome_kind.value,
                    Jsonb(outcome_payload),
                    next_state,
                    instance_id,
                ),
            )
            conn.commit()

    def close_state_instance(self, instance_id: int, *, now: dt.datetime) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE state_instances SET exited_at = %s WHERE id = %s",
                (_to_db(now), instance_id),
            )
            conn.commit()

    def record_failure(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        failure_phase: str,
        failure_reason: str,
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE state_instances
                   SET failure_phase = %s, failure_reason = %s
                 WHERE id = %s
                """,
                (failure_phase, failure_reason, instance_id),
            )
            conn.commit()

    def list_in_flight_state_instances(self) -> list[StateInstanceRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM state_instances WHERE exited_at IS NULL ORDER BY id"
            ).fetchall()
        return [_instance_from_row(r) for r in rows]

    def list_state_instances_for_ticket(self, ticket_id: int) -> list[StateInstanceRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM state_instances WHERE ticket_id = %s ORDER BY sequence",
                (ticket_id,),
            ).fetchall()
        return [_instance_from_row(r) for r in rows]
