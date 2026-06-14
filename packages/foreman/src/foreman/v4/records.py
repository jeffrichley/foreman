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


@dataclass(frozen=True, slots=True)
class TicketRecord:
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

    @property
    def is_held(self) -> bool:
        return self.held_by is not None


@dataclass(frozen=True, slots=True)
class StateInstanceRecord:
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

    @property
    def is_in_flight(self) -> bool:
        return self.exited_at is None
