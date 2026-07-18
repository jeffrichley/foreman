"""PostgresTicketRepository — production persistence on PostgreSQL.

Behavioral parity with InMemoryTicketRepository — the same
RepositoryContract test suite runs against both. Synchronous, matching
v4's ThreadPoolExecutor-based daemon. A psycopg_pool.ConnectionPool
serves the worker threads; Postgres MVCC removes write contention, so
there is no lock here.

Integer PKs (BIGSERIAL) preserved — the Protocol types ids as int.

Datetime contract: callers pass naive dt.datetime via ``now=`` (the
contract's ``_now()`` and the literal assertions like
``dt.datetime(2026, 6, 13, 13)`` are all naive). The in-memory impl
round-trips a naive input to the identical naive datetime, so equality
holds. Postgres TIMESTAMPTZ
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
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from foreman.v4.outcome import OutcomeKind, is_runaway_exempt, is_transient_error_exempt
from foreman.v4.records import (
    StateInstanceRecord,
    TicketRecord,
)
from foreman.v4.repository import (
    MergeQueueEntry,
    StateInstanceNotFoundError,
    TicketAlreadyExistsError,
    TicketNotFoundError,
)

_SCHEMA = Path(__file__).with_name("postgres_schema.sql")


def _to_db(value: dt.datetime | None) -> dt.datetime | None:
    """Normalize a caller datetime for TIMESTAMPTZ storage.

    Naive datetimes are assumed UTC, so round-tripping is stable and
    matches the RepositoryContract behavior.
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
    naive value the in-memory impl would have returned.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(dt.UTC).replace(tzinfo=None)
    return value


def _from_db_required(value: dt.datetime) -> dt.datetime:
    """Strip tzinfo from a NOT NULL TIMESTAMPTZ column read.

    ``created_at`` / ``updated_at`` / ``entered_at`` are NOT NULL in the
    schema, so the row value is always a datetime. This narrows the
    optional ``_from_db`` return for those required columns, keeping the
    record dataclass fields (which type them as non-optional
    ``dt.datetime``) accurately typed without a cast.
    """
    if value.tzinfo is not None:
        value = value.astimezone(dt.UTC).replace(tzinfo=None)
    return value


def _ticket_from_row(row: dict[str, Any]) -> TicketRecord:
    return TicketRecord(
        id=row["id"],
        project=row["project"],
        issue_number=row["issue_number"],
        current_state=row["current_state"],
        created_at=_from_db_required(row["created_at"]),
        updated_at=_from_db_required(row["updated_at"]),
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
        entered_at=_from_db_required(row["entered_at"]),
        execute_started_at=_from_db(row["execute_started_at"]),
        execute_completed_at=_from_db(row["execute_completed_at"]),
        exited_at=_from_db(row["exited_at"]),
        outcome_kind=kind,
        outcome_payload=row["outcome_payload"],
        next_state=row["next_state"],
        failure_phase=row["failure_phase"],
        failure_reason=row["failure_reason"],
        session_id=row["session_id"],
    )


def _merge_entry_from_row(row: dict[str, Any]) -> MergeQueueEntry:
    return MergeQueueEntry(
        id=row["id"],
        project=row["project"],
        ticket_id=row["ticket_id"],
        pr_number=row["pr_number"],
        kind=row["kind"],
        status=row["status"],
        attempts=row["attempts"],
        enqueued_at=_from_db_required(row["enqueued_at"]),
    )


_TERMINAL_STATES = ("Done", "Failed")


class PostgresTicketRepository:
    """Production `TicketRepository` backed by a PostgreSQL database.

    Each method borrows a connection from the injected `ConnectionPool`,
    runs a single statement or short transaction, and commits (or rolls
    back on a caught error) before returning — there is no long-lived
    transaction spanning multiple public calls. Postgres MVCC handles
    concurrent access from the daemon's worker threads, so no
    application-level locking is needed.
    """

    def __init__(self, pool: ConnectionPool[psycopg.Connection[DictRow]]) -> None:
        self._pool = pool
        with self._pool.connection() as conn:
            conn.execute(_SCHEMA.read_text(encoding="utf-8"))
            conn.commit()

    @classmethod
    def from_dsn(
        cls, dsn: str, *, pool_min: int = 2, pool_max: int = 10
    ) -> PostgresTicketRepository:
        """Build a repository from a DSN, opening its own connection pool.

        The pool is created with ``dict_row`` so query results come back
        as ``dict[str, Any]`` rows (what `_ticket_from_row` /
        `_instance_from_row` expect) and ``autocommit=False`` so each
        method controls its own commit/rollback boundary.
        """
        pool: ConnectionPool[psycopg.Connection[DictRow]] = ConnectionPool(
            dsn,
            min_size=pool_min,
            max_size=pool_max,
            connection_class=psycopg.Connection[DictRow],
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
        return cls(pool)

    def close(self) -> None:
        """Close the underlying connection pool, releasing all connections."""
        self._pool.close()

    # --- Ticket CRUD ---

    def create_ticket(self, *, project: str, issue_number: int, now: dt.datetime) -> TicketRecord:
        """Insert a new Queued ticket row, translating a duplicate-key error.

        The ``(project, issue_number)`` uniqueness rule is enforced by a
        database constraint rather than a pre-check query, so a
        `psycopg.errors.UniqueViolation` on insert is caught, the
        transaction rolled back, and re-raised as
        `TicketAlreadyExistsError` to match the in-memory implementation.
        """
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
        """Select the ticket row by primary key, translating a miss to a domain error."""
        with self._pool.connection() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id = %s", (ticket_id,)).fetchone()
        if row is None:
            raise TicketNotFoundError(str(ticket_id))
        return _ticket_from_row(row)

    def get_ticket_by_issue(self, *, project: str, issue_number: int) -> TicketRecord:
        """Select the ticket row by its ``(project, issue_number)`` unique index."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM tickets WHERE project = %s AND issue_number = %s",
                (project, issue_number),
            ).fetchone()
        if row is None:
            raise TicketNotFoundError(f"{project}#{issue_number}")
        return _ticket_from_row(row)

    def list_open_tickets(self) -> list[TicketRecord]:
        """Select every ticket whose state isn't in the terminal-states array.

        Uses Postgres's ``<> ALL(%s)`` array-comparison form rather than
        ``NOT IN``: psycopg adapts a Python list to a native array, and
        ``NOT IN %s`` with a tuple binds to a single placeholder that
        Postgres rejects as a syntax error.
        """
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
        """Select every ticket row, ordered by id."""
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()
        return [_ticket_from_row(r) for r in rows]

    def set_ticket_state(self, ticket_id: int, new_state: str, *, now: dt.datetime) -> None:
        """Update the ticket's state and ``updated_at``, rolling back if the id is unknown.

        Existence is inferred from the ``UPDATE``'s own affected-row
        count rather than a separate ``SELECT``, saving a round trip; a
        zero rowcount rolls back the (no-op) transaction and raises
        `TicketNotFoundError`.
        """
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
        """Update the ticket's held_by/held_at/held_reason columns, or raise if unknown."""
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
        """Null out the ticket's held_by/held_at/held_reason columns, or raise if unknown."""
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
        """Delete the ticket row, relying on a DB-level cascade for its journal.

        Unlike the in-memory implementation, which manually filters out
        orphaned state-instance rows, here ``ON DELETE CASCADE`` on
        ``state_instances.ticket_id`` drops the journal rows as part of
        the same statement.
        """
        # ON DELETE CASCADE on state_instances.ticket_id drops the journal
        # rows automatically (matches InMemory's manual cascade).
        with self._pool.connection() as conn:
            cur = conn.execute("DELETE FROM tickets WHERE id = %s", (ticket_id,))
            if cur.rowcount == 0:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            conn.commit()

    def set_next_action_at(self, ticket_id: int, *, when: dt.datetime) -> None:
        """Update ``next_action_at`` and ``updated_at`` to ``when``, or raise if unknown."""
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
        """Null out ``next_action_at`` for the ticket.

        Unlike its sibling mutators, this does not check the affected-row
        count or raise `TicketNotFoundError` for an unknown id — an
        unconditional no-op UPDATE matches the in-memory implementation's
        own no-op-on-already-clear behavior and the Protocol declares no
        error for this method.
        """
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
        """Insert a new journal row, checking ticket existence before the insert.

        The ``ticket_id`` foreign key would itself reject an insert for
        an unknown ticket, but that failure surfaces as a generic FK
        violation; a preceding existence check lets this raise the clean
        `TicketNotFoundError` the contract expects instead.
        """
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
        """Select the state-instance row by primary key, translating a miss to a domain error."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM state_instances WHERE id = %s", (instance_id,)
            ).fetchone()
        if row is None:
            raise StateInstanceNotFoundError(str(instance_id))
        return _instance_from_row(row)

    def mark_execute_started(self, instance_id: int, *, now: dt.datetime) -> None:
        """Update ``execute_started_at`` on the instance row."""
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
        """Update the instance with its execute() outcome, storing the payload as JSONB.

        The outcome payload is wrapped in `psycopg.types.json.Jsonb` so
        psycopg serializes the dict for the ``jsonb`` column, and the
        `OutcomeKind` enum is stored by its string ``.value`` rather than
        the enum member itself.
        """
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
        """Update ``exited_at`` on the instance row, closing its lifecycle."""
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
        """Update ``failure_phase`` and ``failure_reason`` on the instance row."""
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

    def set_session_id(self, instance_id: int, session_id: str) -> None:
        """Update the instance's ``session_id`` column."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE state_instances SET session_id = %s WHERE id = %s",
                (session_id, instance_id),
            )
            conn.commit()

    def list_in_flight_state_instances(self) -> list[StateInstanceRecord]:
        """Select every state-instance row across all tickets with a NULL ``exited_at``."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM state_instances WHERE exited_at IS NULL ORDER BY id"
            ).fetchall()
        return [_instance_from_row(r) for r in rows]

    def list_state_instances_for_ticket(self, ticket_id: int) -> list[StateInstanceRecord]:
        """Select every state-instance row for the ticket, ordered by sequence."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM state_instances WHERE ticket_id = %s ORDER BY sequence",
                (ticket_id,),
            ).fetchall()
        return [_instance_from_row(r) for r in rows]

    def append_event(
        self,
        *,
        ticket_id: int,
        instance_id: int,
        event_type: str,
        state_name: str,
        sequence: int,
        at: dt.datetime,
        payload: dict[str, Any],
    ) -> None:
        """Insert an audit-log row, storing ``payload`` as JSONB via `Jsonb`."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO events (ticket_id, instance_id, event_type, "
                "state_name, sequence, at, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    ticket_id,
                    instance_id,
                    event_type,
                    state_name,
                    sequence,
                    _to_db(at),
                    Jsonb(payload),
                ),
            )
            conn.commit()

    def list_events_for_ticket(self, ticket_id: int) -> list[dict[str, Any]]:
        """Select every event row for the ticket, ordered by timestamp."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE ticket_id = %s ORDER BY at",
                (ticket_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- Helpers used by states / WorkerPool / QueueManager ---

    def latest_pr_number_for_ticket(self, ticket_id: int) -> int | None:
        """Return the most recent ``pr_number`` recorded for a ticket, or None.

        Fetches every instance's ``outcome_payload`` newest-first and
        scans them in Python for the first populated ``pr_number`` — the
        lookup is nested inside a JSONB blob rather than its own column,
        so there's no indexable SQL predicate to push this filter into
        the query itself.
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT outcome_payload FROM state_instances "
                "WHERE ticket_id = %s ORDER BY sequence DESC",
                (ticket_id,),
            ).fetchall()
        for row in rows:
            payload = row["outcome_payload"]
            if not payload:
                continue
            pr_number = (payload or {}).get("artifacts", {}).get("pr_number")
            if pr_number is not None:
                return int(pr_number)
        return None

    def count_state_instances_for_ticket(self, ticket_id: int) -> int:
        """Return ``COUNT(*)`` of state-instance rows for the ticket."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM state_instances WHERE ticket_id = %s",
                (ticket_id,),
            ).fetchone()
        assert row is not None
        return int(row["n"])

    def count_consecutive_same_state(self, *, ticket_id: int, state: str) -> int:
        """Count consecutive newest-first instances of the ticket still in ``state``.

        Reuses `list_state_instances_for_ticket` and reverses it in
        Python rather than issuing a ``DESC``-ordered query, then mirrors
        `InMemoryTicketRepository.count_consecutive_same_state` exactly:
        walk newest-first, delegate the skip decision to
        :func:`~foreman.v4.outcome.is_runaway_exempt`, count matching
        ``state_name`` rows, and break on the first non-matching one.
        """
        # Mirror InMemoryTicketRepository.count_consecutive_same_state exactly:
        # walk newest-first, delegate skip decision to is_runaway_exempt, count
        # matching state_name rows, break on first non-matching.
        instances = self.list_state_instances_for_ticket(ticket_id)
        instances.reverse()  # sequence DESC
        count = 0
        for inst in instances:
            if is_runaway_exempt(inst.failure_phase, inst.outcome_kind):
                continue
            if inst.state_name == state:
                count += 1
            else:
                break
        return count

    def count_consecutive_transient_provider_errors(self, ticket_id: int) -> int:
        """Count consecutive newest-first transient-provider-error outcomes.

        Mirrors `InMemoryTicketRepository.count_consecutive_transient_provider_errors`
        exactly: delegates the skip decision to
        :func:`~foreman.v4.outcome.is_transient_error_exempt`, counts
        consecutive ``TRANSIENT_PROVIDER_ERROR`` outcomes, and breaks on
        any other completed outcome.
        """
        # Mirror InMemoryTicketRepository exactly: delegate skip decision to
        # is_transient_error_exempt; count consecutive TRANSIENT_PROVIDER_ERROR;
        # break on any other completed outcome.
        instances = self.list_state_instances_for_ticket(ticket_id)
        instances.reverse()
        count = 0
        for inst in instances:
            if is_transient_error_exempt(inst.failure_phase, inst.outcome_kind):
                continue
            if inst.outcome_kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR:
                count += 1
            else:
                break
        return count

    # --- Dependency tracking ---

    def set_ticket_dependencies(self, ticket_id: int, *, deps: list[int]) -> None:
        """Replace the ticket's ``depends_on`` JSONB array with ``deps``."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE tickets SET depends_on = %s WHERE id = %s",
                (Jsonb(list(deps)), ticket_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise TicketNotFoundError(str(ticket_id))
            conn.commit()

    def get_ticket_dependencies(self, ticket_id: int) -> list[int]:
        """Return the ticket's ``depends_on`` array by delegating to `get_ticket`."""
        return list(self.get_ticket(ticket_id).depends_on)

    def list_unmet_dependencies(self, ticket_id: int) -> list[int]:
        """Return the stored ``depends_on`` list verbatim.

        The reconciler guarantees ``depends_on`` holds only currently-unmet
        dep issue numbers, so no Done-state filtering is needed here.
        Returning the stored list directly also avoids crashing when
        ``depends_on`` contains issue numbers of untracked GitHub issues.
        """
        return list(self.get_ticket(ticket_id).depends_on)

    # --- Merge queue (foreman#550) ---

    def enqueue_merge(
        self, *, project: str, ticket_id: int, pr_number: int, kind: str, now: dt.datetime
    ) -> MergeQueueEntry:
        """Insert a new ``queued`` merge_queue row and return it."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO merge_queue
                    (project, ticket_id, pr_number, kind, status, attempts, enqueued_at)
                VALUES (%s, %s, %s, %s, 'queued', 0, %s)
                RETURNING *
                """,
                (project, ticket_id, pr_number, kind, _to_db(now)),
            ).fetchone()
            conn.commit()
            assert row is not None
            return _merge_entry_from_row(row)

    def merge_queue_for_project(self, project: str) -> list[MergeQueueEntry]:
        """Select the project's merge_queue rows, FIFO-ordered by ``enqueued_at``, then ``id``.

        ``id`` is a secondary sort key so entries enqueued with an
        identical ``enqueued_at`` (the coordinator's clock can produce
        ties) still order deterministically.
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM merge_queue WHERE project = %s ORDER BY enqueued_at ASC, id ASC",
                (project,),
            ).fetchall()
        return [_merge_entry_from_row(r) for r in rows]

    def head_merge_entry(self, project: str) -> MergeQueueEntry | None:
        """Select the project's earliest merge_queue row, or None if its queue is empty."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM merge_queue WHERE project = %s "
                "ORDER BY enqueued_at ASC, id ASC LIMIT 1",
                (project,),
            ).fetchone()
        return _merge_entry_from_row(row) if row is not None else None

    def mark_merge_active(self, entry_id: int) -> None:
        """Update the entry's ``status`` to ``'merging'``.

        Raises:
            LookupError: no merge_queue entry with ``entry_id`` exists.
        """
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE merge_queue SET status = 'merging' WHERE id = %s",
                (entry_id,),
            )
            if cur.rowcount == 0:
                conn.rollback()
                raise LookupError(str(entry_id))
            conn.commit()

    def increment_merge_attempts(self, entry_id: int) -> int:
        """Increment the entry's ``attempts`` column by one and return the new count.

        Raises:
            LookupError: no merge_queue entry with ``entry_id`` exists. A
                zero-row ``UPDATE ... RETURNING`` is a legitimate outcome
                for an unknown id, not an invariant violation, so it is
                translated to this domain error rather than asserted away.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE merge_queue SET attempts = attempts + 1 WHERE id = %s RETURNING attempts",
                (entry_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise LookupError(str(entry_id))
            conn.commit()
        return int(row["attempts"])

    def dequeue_merge(self, entry_id: int) -> None:
        """Delete the entry from merge_queue."""
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM merge_queue WHERE id = %s", (entry_id,))
            conn.commit()

    def list_active_merges(self) -> list[MergeQueueEntry]:
        """Select every merge_queue row, across all projects, whose status is ``'merging'``."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM merge_queue WHERE status = 'merging' ORDER BY id"
            ).fetchall()
        return [_merge_entry_from_row(r) for r in rows]
