"""SqliteTicketRepository — production persistence backed by stdlib sqlite3.

Behavior contract is identical to InMemoryTicketRepository — the same
RepositoryContract test suite runs against both. If this file diverges, the
contract tests catch it before anything downstream notices.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from foreman.v4.outcome import OutcomeKind
from foreman.v4.records import (
    FAILURE_PHASE_CRASH_RECOVERY,
    StateInstanceRecord,
    TicketRecord,
)
from foreman.v4.repository import (
    StateInstanceNotFoundError,
    TicketAlreadyExistsError,
    TicketNotFoundError,
)

_SCHEMA = Path(__file__).with_name("schema.sql")
_TERMINAL_STATES = ("Done", "Failed")


def _to_iso(value: dt.datetime) -> str:
    return value.isoformat()


def _from_iso(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value is not None else None


def _ticket_row_to_record(row: sqlite3.Row) -> TicketRecord:
    created_at = _from_iso(row["created_at"])
    updated_at = _from_iso(row["updated_at"])
    assert created_at is not None  # NOT NULL in schema
    assert updated_at is not None  # NOT NULL in schema
    # foreman#361: next_action_at is nullable + added by the additive
    # migration in __init__. Use a defensive lookup so a pre-migration
    # cursor row (no such column) doesn't crash; the ALTER TABLE in
    # __init__ guarantees the column exists by the time we read.
    try:
        next_action_at_raw = row["next_action_at"]
    except (KeyError, IndexError):
        next_action_at_raw = None
    return TicketRecord(
        id=row["id"],
        project=row["project"],
        issue_number=row["issue_number"],
        current_state=row["current_state"],
        created_at=created_at,
        updated_at=updated_at,
        held_by=row["held_by"],
        held_at=_from_iso(row["held_at"]),
        held_reason=row["held_reason"],
        depends_on=list(json.loads(row["depends_on"])),
        next_action_at=_from_iso(next_action_at_raw),
    )


def _instance_row_to_record(row: sqlite3.Row) -> StateInstanceRecord:
    payload_raw = row["outcome_payload"]
    payload: dict[str, Any] | None = json.loads(payload_raw) if payload_raw else None
    kind = OutcomeKind(row["outcome_kind"]) if row["outcome_kind"] else None
    entered_at = _from_iso(row["entered_at"])
    assert entered_at is not None  # NOT NULL in schema
    return StateInstanceRecord(
        id=row["id"],
        ticket_id=row["ticket_id"],
        state_name=row["state_name"],
        sequence=row["sequence"],
        entered_at=entered_at,
        execute_started_at=_from_iso(row["execute_started_at"]),
        execute_completed_at=_from_iso(row["execute_completed_at"]),
        exited_at=_from_iso(row["exited_at"]),
        outcome_kind=kind,
        outcome_payload=payload,
        next_state=row["next_state"],
        failure_phase=row["failure_phase"],
        failure_reason=row["failure_reason"],
    )


class SqliteTicketRepository:
    """Production TicketRepository backed by stdlib sqlite3.

    Behavior must match InMemoryTicketRepository — the shared
    RepositoryContract suite runs against both. If you find a behavior gap,
    the bug is in whichever impl diverges from the contract.

    Thread-safe under the WorkerPool's usage pattern: the classmethod
    constructors set check_same_thread=False and WAL mode, so N worker
    threads can call repo methods concurrently. SQLite serializes writers
    at the engine level; readers don't block writers under WAL.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
        # WAL mode is required for the ThreadPoolExecutor-based WorkerPool
        # (Phase 4 Task 4.4) — rollback-journal mode would deadlock on
        # concurrent writes from N worker threads. synchronous=NORMAL is the
        # recommended pairing — safe against power loss, faster than FULL.
        # Note: :memory: databases silently ignore WAL; the RLock below is
        # what actually serializes concurrent access for those.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.commit()
        # foreman#361: additive ``next_action_at`` migration. SQLite's
        # ``CREATE TABLE IF NOT EXISTS`` (used by executescript above)
        # is a no-op when the table already exists, so a pre-existing
        # on-disk DB created against the old schema would NEVER get
        # the new column from the inline declaration alone — the
        # ALTER TABLE is load-bearing for forward compatibility. No
        # existing additive-migration precedent exists in this repo
        # (the depends_on column was added by inline schema
        # declaration BEFORE any production DB existed); this ticket
        # introduces the pattern. Future column adds should mirror
        # this shape.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
        if "next_action_at" not in cols:
            conn.execute("ALTER TABLE tickets ADD COLUMN next_action_at TEXT")
            conn.commit()
        self._conn = conn
        # RLock serializes every public method. WAL alone isn't enough on
        # :memory: (it silently falls back to "memory" journal mode), and
        # mixing read/write SQL from N worker threads on a shared cursor
        # corrupts row_factory state. Coarse-grained but cheap: every
        # public method is microseconds long.
        self._lock = threading.RLock()

    @classmethod
    def in_memory(cls) -> SqliteTicketRepository:
        # check_same_thread=False so the WorkerPool (Phase 4 Task 4.4) can
        # call repo methods from N worker threads. WAL mode (set in __init__)
        # serializes writers at the SQLite engine level; combined with the
        # rarity of long write transactions in v4, this is safe in practice.
        return cls(sqlite3.connect(":memory:", check_same_thread=False))

    @classmethod
    def at_path(cls, path: Path) -> SqliteTicketRepository:
        return cls(sqlite3.connect(path, check_same_thread=False))

    @property
    def connection(self) -> sqlite3.Connection:
        """Read-only handle to the underlying SQLite connection.

        Exposed so bootstrap can pass it to ``EventArchiveObserver``
        without reaching into ``_conn``. Callers MUST treat this as the
        shared connection — additional writes from outside the
        repository are still serialized by the same RLock-less SQLite
        engine layer, so observer writes (single ``INSERT INTO events``
        per event) are safe under WAL.
        """
        return self._conn

    # --- Ticket CRUD ---

    def create_ticket(self, *, project: str, issue_number: int, now: dt.datetime) -> TicketRecord:
        with self._lock:
            ts = _to_iso(now)
            try:
                cur = self._conn.execute(
                    "INSERT INTO tickets(project, issue_number, current_state, created_at, updated_at) "
                    "VALUES (?, ?, 'Queued', ?, ?)",
                    (project, issue_number, ts, ts),
                )
            except sqlite3.IntegrityError as exc:
                raise TicketAlreadyExistsError(f"{project}#{issue_number}") from exc
            self._conn.commit()
            assert cur.lastrowid is not None
            return self.get_ticket(cur.lastrowid)

    def get_ticket(self, ticket_id: int) -> TicketRecord:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if row is None:
                raise TicketNotFoundError(str(ticket_id))
            return _ticket_row_to_record(row)

    def get_ticket_by_issue(self, *, project: str, issue_number: int) -> TicketRecord:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tickets WHERE project = ? AND issue_number = ?",
                (project, issue_number),
            ).fetchone()
            if row is None:
                raise TicketNotFoundError(f"{project}#{issue_number}")
            return _ticket_row_to_record(row)

    def list_open_tickets(self) -> list[TicketRecord]:
        with self._lock:
            placeholders = ",".join(["?"] * len(_TERMINAL_STATES))
            rows = self._conn.execute(
                f"SELECT * FROM tickets WHERE current_state NOT IN ({placeholders})",
                _TERMINAL_STATES,
            ).fetchall()
            return [_ticket_row_to_record(r) for r in rows]

    def list_all_tickets(self) -> list[TicketRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()
            return [_ticket_row_to_record(r) for r in rows]

    def set_ticket_state(self, ticket_id: int, new_state: str, *, now: dt.datetime) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tickets SET current_state = ?, updated_at = ? WHERE id = ?",
                (new_state, _to_iso(now), ticket_id),
            )
            self._conn.commit()

    def hold_ticket(self, ticket_id: int, *, held_by: str, reason: str, now: dt.datetime) -> None:
        with self._lock:
            ts = _to_iso(now)
            self._conn.execute(
                "UPDATE tickets SET held_by = ?, held_at = ?, held_reason = ?, updated_at = ? "
                "WHERE id = ?",
                (held_by, ts, reason, ts, ticket_id),
            )
            self._conn.commit()

    def resume_ticket(self, ticket_id: int, *, now: dt.datetime) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tickets SET held_by = NULL, held_at = NULL, held_reason = NULL, "
                "updated_at = ? WHERE id = ?",
                (_to_iso(now), ticket_id),
            )
            self._conn.commit()

    def set_next_action_at(self, ticket_id: int, *, when: dt.datetime) -> None:
        with self._lock:
            ts = _to_iso(when)
            self._conn.execute(
                "UPDATE tickets SET next_action_at = ?, updated_at = ? WHERE id = ?",
                (ts, ts, ticket_id),
            )
            self._conn.commit()

    def clear_next_action_at(self, ticket_id: int) -> None:
        with self._lock:
            now_ts = _to_iso(dt.datetime.now(dt.UTC))
            self._conn.execute(
                "UPDATE tickets SET next_action_at = NULL, updated_at = ? WHERE id = ?",
                (now_ts, ticket_id),
            )
            self._conn.commit()

    def delete_ticket(self, ticket_id: int) -> None:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "SELECT 1 FROM tickets WHERE id = ?",
                (ticket_id,),
            )
            if cur.fetchone() is None:
                raise TicketNotFoundError(str(ticket_id))
            self._conn.execute(
                "DELETE FROM state_instances WHERE ticket_id = ?",
                (ticket_id,),
            )
            self._conn.execute(
                "DELETE FROM tickets WHERE id = ?",
                (ticket_id,),
            )

    # --- State-instance journal ---

    def open_state_instance(
        self,
        *,
        ticket_id: int,
        state_name: str,
        sequence: int,
        now: dt.datetime,
    ) -> StateInstanceRecord:
        with self._lock:
            self.get_ticket(ticket_id)  # raise if missing
            cur = self._conn.execute(
                "INSERT INTO state_instances(ticket_id, state_name, sequence, entered_at) "
                "VALUES (?, ?, ?, ?)",
                (ticket_id, state_name, sequence, _to_iso(now)),
            )
            self._conn.commit()
            assert cur.lastrowid is not None
            return self.get_state_instance(cur.lastrowid)

    def get_state_instance(self, instance_id: int) -> StateInstanceRecord:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM state_instances WHERE id = ?", (instance_id,)
            ).fetchone()
            if row is None:
                raise StateInstanceNotFoundError(str(instance_id))
            return _instance_row_to_record(row)

    def mark_execute_started(self, instance_id: int, *, now: dt.datetime) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE state_instances SET execute_started_at = ? WHERE id = ?",
                (_to_iso(now), instance_id),
            )
            self._conn.commit()

    def mark_execute_completed(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        outcome_kind: OutcomeKind,
        outcome_payload: dict[str, Any],
        next_state: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE state_instances "
                "SET execute_completed_at = ?, outcome_kind = ?, outcome_payload = ?, next_state = ? "
                "WHERE id = ?",
                (
                    _to_iso(now),
                    outcome_kind.value,
                    json.dumps(outcome_payload),
                    next_state,
                    instance_id,
                ),
            )
            self._conn.commit()

    def close_state_instance(self, instance_id: int, *, now: dt.datetime) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE state_instances SET exited_at = ? WHERE id = ?",
                (_to_iso(now), instance_id),
            )
            self._conn.commit()

    def record_failure(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        failure_phase: str,
        failure_reason: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE state_instances SET failure_phase = ?, failure_reason = ? WHERE id = ?",
                (failure_phase, failure_reason, instance_id),
            )
            self._conn.commit()

    def list_in_flight_state_instances(self) -> list[StateInstanceRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM state_instances WHERE exited_at IS NULL ORDER BY ticket_id, sequence"
            ).fetchall()
            return [_instance_row_to_record(r) for r in rows]

    def list_state_instances_for_ticket(
        self,
        ticket_id: int,
    ) -> list[StateInstanceRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM state_instances WHERE ticket_id = ? ORDER BY sequence",
                (ticket_id,),
            ).fetchall()
            return [_instance_row_to_record(r) for r in rows]

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
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ticket_id, instance_id, event_type, "
                "state_name, sequence, at, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ticket_id,
                    instance_id,
                    event_type,
                    state_name,
                    sequence,
                    _to_iso(at),
                    json.dumps(payload),
                ),
            )
            self._conn.commit()

    def list_events_for_ticket(self, ticket_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE ticket_id = ? ORDER BY at",
                (ticket_id,),
            ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    # --- Helpers used by states / WorkerPool / QueueManager ---

    def latest_pr_number_for_ticket(self, ticket_id: int) -> int | None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT outcome_payload FROM state_instances "
                "WHERE ticket_id = ? AND outcome_payload IS NOT NULL "
                "ORDER BY sequence DESC",
                (ticket_id,),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["outcome_payload"])
                pr_number = payload.get("artifacts", {}).get("pr_number")
                if pr_number is not None:
                    return int(pr_number)
            return None

    def count_state_instances_for_ticket(self, ticket_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM state_instances WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
            return int(row["n"])

    def count_consecutive_same_state(self, *, ticket_id: int, state: str) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state_name, failure_phase, outcome_kind "
                "FROM state_instances "
                "WHERE ticket_id = ? "
                "ORDER BY sequence DESC",
                (ticket_id,),
            ).fetchall()
            count = 0
            for row in rows:
                # Phase 8d.15 (F4): can_run-failed rows record "the
                # ticket was held and the state never executed" — they
                # are not runaway-defense signal. Skip them entirely
                # (neither count nor break the run) so a held-then-
                # resumed ticket doesn't immediately escalate to
                # NeedsHelp.
                if row["failure_phase"] == "can_run":
                    continue
                # A daemon restart closed this orphan as crash_recovery; it
                # is not runaway-defense signal. Skip (neither count nor
                # break).
                if row["failure_phase"] == FAILURE_PHASE_CRASH_RECOVERY:
                    continue
                # Phase 8d.18: BLOCKED-outcome rows record a legitimate
                # async-polling self-loop (MergingState polling a
                # pending merge verdict; ImplementingState polling
                # impl-PR CI). The state ran, emitted "still waiting",
                # and asked to be re-tried via next_state() → self.
                # They are not runaway-defense signal. Skip them
                # entirely so a few polling cycles don't trip the cap
                # and escalate the ticket to NeedsHelp.
                if row["outcome_kind"] == OutcomeKind.BLOCKED.value:
                    continue
                # foreman#361 — transient-provider self-loops are not
                # runaway-defense signal. Same precedent as the
                # BLOCKED skip above: the role hit an Anthropic-side
                # blip, the state machine's backoff scheduler owns
                # the retry budget, and a short outage must not trip
                # the cap and escalate by the wrong path.
                if row["outcome_kind"] == OutcomeKind.TRANSIENT_PROVIDER_ERROR.value:
                    continue
                if row["state_name"] == state:
                    count += 1
                else:
                    break
            return count

    def count_consecutive_transient_provider_errors(self, ticket_id: int) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT failure_phase, outcome_kind "
                "FROM state_instances "
                "WHERE ticket_id = ? "
                "ORDER BY sequence DESC",
                (ticket_id,),
            ).fetchall()
            count = 0
            for row in rows:
                # foreman#361: same precedent as
                # count_consecutive_same_state — can_run failures
                # don't count and don't break.
                if row["failure_phase"] == "can_run":
                    continue
                # foreman#361 CRITICAL: skip the in-flight row.
                # ``RoleDispatchState.next_state`` is called BEFORE
                # ``mark_execute_completed`` writes the outcome_kind,
                # so the most-recent row has outcome_kind=NULL when
                # we walk from inside the Template Method. Without
                # this skip, every call would see "current row,
                # NULL outcome, break" and return 0 — the backoff
                # would never advance past 30s.
                if row["outcome_kind"] is None:
                    continue
                if row["outcome_kind"] == OutcomeKind.TRANSIENT_PROVIDER_ERROR.value:
                    count += 1
                else:
                    # Any other completed outcome breaks the run.
                    break
            return count

    # --- Dependency tracking ---

    def set_ticket_dependencies(self, ticket_id: int, *, deps: list[int]) -> None:
        with self._lock:
            self.get_ticket(ticket_id)  # raise TicketNotFoundError if missing
            self._conn.execute(
                "UPDATE tickets SET depends_on = ? WHERE id = ?",
                (json.dumps(list(deps)), ticket_id),
            )
            self._conn.commit()

    def get_ticket_dependencies(self, ticket_id: int) -> list[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT depends_on FROM tickets WHERE id = ?",
                (ticket_id,),
            ).fetchone()
            if row is None:
                raise TicketNotFoundError(str(ticket_id))
            return list(json.loads(row["depends_on"]))

    def list_unmet_dependencies(self, ticket_id: int) -> list[int]:
        with self._lock:
            deps = self.get_ticket_dependencies(ticket_id)
            if not deps:
                return []
            placeholders = ",".join(["?"] * len(deps))
            rows = self._conn.execute(
                f"SELECT id, current_state FROM tickets WHERE id IN ({placeholders})",
                deps,
            ).fetchall()
            found_ids = {r["id"] for r in rows}
            missing = [d for d in deps if d not in found_ids]
            if missing:
                raise TicketNotFoundError(str(missing[0]))
            return [r["id"] for r in rows if r["current_state"] != "Done"]
