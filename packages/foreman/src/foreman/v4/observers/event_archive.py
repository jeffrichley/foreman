"""EventArchiveObserver — append-only events table for forensics + replay.

This is the audit trail. The state_instances journal already persists
the durability story; this observer captures the SAME events as a flat,
append-only log that's easy to grep, easy to replay, and never under
contention from the transition path. If this write fails, the EventBus
isolates the exception; the journal stays correct.
"""

from __future__ import annotations

import json
import sqlite3

from foreman.v4.events import (
    DaemonEvent,
    Event,
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
    TicketEvent,
)

_EVENT_TYPE_NAMES = {
    StateEnteredEvent:     "state_entered",
    ExecuteStartedEvent:   "execute_started",
    ExecuteCompletedEvent: "execute_completed",
    StateExitedEvent:      "state_exited",
    StateFailedEvent:      "state_failed",
}


def _payload_for(event: TicketEvent) -> dict:
    if isinstance(event, ExecuteCompletedEvent):
        return {
            "outcome_kind": event.outcome.kind.value,
            "confidence": event.outcome.confidence.value,
            "summary": event.outcome.summary,
            "next_state": event.next_state,
            # Phase 8d.17 / foreman#315: archive the role's
            # diagnostic detail bag too so the events table is a
            # complete forensic record. Without this, the audit log
            # carries only the terse summary and operators have to
            # spelunk the state_instances.outcome_payload row + the
            # original worktree to recover what actually happened.
            "details": event.outcome.details,
        }
    if isinstance(event, StateExitedEvent):
        return {
            "outcome_kind": event.outcome.kind.value if event.outcome else None,
        }
    if isinstance(event, StateFailedEvent):
        return {
            "failure_phase": event.failure_phase,
            "failure_reason": event.failure_reason,
        }
    return {}


class EventArchiveObserver:
    def __init__(self, *, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __call__(self, event: Event) -> None:
        # Issue #360: the events table requires ``ticket_id INTEGER NOT
        # NULL`` (schema.sql:53). Daemon-level events
        # (:class:`DaemonEvent` subclasses, e.g. BackupTakenEvent /
        # BackupFailedEvent) have no ticket scope, so writing them
        # would violate the constraint. Skip them entirely — the
        # structured-log JSONL is the durable record for daemon-level
        # events; the events SQL table stays ticket-scoped.
        if isinstance(event, DaemonEvent):
            return
        assert isinstance(event, TicketEvent), (
            f"unrecognized Event subclass: {type(event).__name__}"
        )
        event_type = _EVENT_TYPE_NAMES.get(type(event), "unknown")
        self._conn.execute(
            "INSERT INTO events"
            "(ticket_id, instance_id, event_type, state_name, sequence, at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.ticket_id, event.instance_id, event_type,
                event.state_name, event.sequence, event.at.isoformat(),
                json.dumps(_payload_for(event), sort_keys=True),
            ),
        )
        self._conn.commit()
