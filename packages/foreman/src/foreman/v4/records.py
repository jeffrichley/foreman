"""Read-shape dataclasses returned by TicketRepository.

Frozen so callers cannot mutate. Mutation goes through repository write
methods, which produce a new record on read. This keeps the persistence
seam discipline clean — the repository owns identity, callers own
intent.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from foreman.v4.outcome import OutcomeKind

#: failure_phase value written by the startup reconciliation pass to a
#: crash-orphaned in-flight row. Exempt from count_consecutive_same_state
#: (a daemon restart is not a ticket failure). Single source of truth so
#: the four sites that reference it can't drift (review I3).
FAILURE_PHASE_CRASH_RECOVERY = "crash_recovery"


@dataclass(frozen=True, slots=True)
class TicketRecord:
    """Read-only snapshot of one ticket's row as returned by TicketRepository."""

    id: int
    project: str
    issue_number: int
    current_state: str
    created_at: dt.datetime
    updated_at: dt.datetime
    held_by: str | None
    held_at: dt.datetime | None
    held_reason: str | None
    depends_on: list[int] = field(default_factory=list)
    # foreman#361: when non-None, the Poller refuses to enqueue this
    # ticket until ``next_action_at <= now``. Set by
    # ``RoleDispatchState.next_state`` after observing a
    # ``TRANSIENT_PROVIDER_ERROR`` outcome; cleared on any
    # non-transient outcome (defense in depth) and explicitly by
    # ``cmd_retry`` so an operator-forced retry bypasses the
    # suspension. ISO 8601 UTC on the wire.
    next_action_at: dt.datetime | None = None

    @property
    def is_held(self) -> bool:
        """Whether an operator hold is currently blocking this ticket from dispatch."""
        return self.held_by is not None


@dataclass(frozen=True, slots=True)
class StateInstanceRecord:
    """Read-only snapshot of one state_instances row — one state's execution attempt."""

    id: int
    ticket_id: int
    state_name: str
    sequence: int
    entered_at: dt.datetime
    execute_started_at: dt.datetime | None
    execute_completed_at: dt.datetime | None
    exited_at: dt.datetime | None
    outcome_kind: OutcomeKind | None
    outcome_payload: dict[str, Any] | None
    next_state: str | None
    failure_phase: str | None
    failure_reason: str | None
    session_id: str | None = None

    @property
    def is_in_flight(self) -> bool:
        """Whether this state instance is still open (has not yet exited)."""
        return self.exited_at is None
