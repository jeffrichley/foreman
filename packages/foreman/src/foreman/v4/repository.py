"""The TicketRepository seam.

Two implementations: InMemoryTicketRepository for tests; SqliteTicketRepository
for production. Domain code talks only to the Protocol — never to sqlite3 or
to the dict storage directly. This is the only place persistence concerns
leak into the v4 codebase.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any, Protocol

from foreman.v4.outcome import OutcomeKind
from foreman.v4.records import StateInstanceRecord, TicketRecord


class TicketNotFoundError(LookupError):
    """No ticket exists with the given id or (project, issue_number)."""


class StateInstanceNotFoundError(LookupError):
    """No state-instance exists with the given id."""


class TicketAlreadyExistsError(ValueError):
    """A ticket with this (project, issue_number) is already tracked."""


class MissingPRNumberError(LookupError):
    """No state outcome on this ticket recorded a pr_number."""


class TicketRepository(Protocol):
    """Persistence contract for tickets and state-instances."""

    # --- Ticket CRUD ---

    def create_ticket(self, *, project: str, issue_number: int, now: dt.datetime) -> TicketRecord: ...
    def get_ticket(self, ticket_id: int) -> TicketRecord: ...
    def get_ticket_by_issue(self, *, project: str, issue_number: int) -> TicketRecord: ...
    def list_open_tickets(self) -> list[TicketRecord]: ...
    def list_all_tickets(self) -> list[TicketRecord]: ...
    def set_ticket_state(self, ticket_id: int, new_state: str, *, now: dt.datetime) -> None: ...
    def hold_ticket(self, ticket_id: int, *, held_by: str, reason: str, now: dt.datetime) -> None: ...
    def resume_ticket(self, ticket_id: int, *, now: dt.datetime) -> None: ...

    # --- State-instance journal ---

    def open_state_instance(
        self, *, ticket_id: int, state_name: str, sequence: int, now: dt.datetime
    ) -> StateInstanceRecord: ...
    def get_state_instance(self, instance_id: int) -> StateInstanceRecord: ...
    def mark_execute_started(self, instance_id: int, *, now: dt.datetime) -> None: ...
    def mark_execute_completed(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        outcome_kind: OutcomeKind,
        outcome_payload: dict[str, Any],
        next_state: str,
    ) -> None: ...
    def close_state_instance(self, instance_id: int, *, now: dt.datetime) -> None: ...
    def record_failure(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        failure_phase: str,
        failure_reason: str,
    ) -> None: ...
    def list_in_flight_state_instances(self) -> list[StateInstanceRecord]: ...
    def list_state_instances_for_ticket(
        self, ticket_id: int,
    ) -> list[StateInstanceRecord]: ...

    # --- Helpers used by states / WorkerPool / QueueManager ---

    def latest_pr_number_for_ticket(self, ticket_id: int) -> int | None: ...
    def count_state_instances_for_ticket(self, ticket_id: int) -> int: ...
    def count_consecutive_same_state(
        self, *, ticket_id: int, state: str
    ) -> int:
        """Return the number of consecutive ``state_instances`` rows for
        ``ticket_id`` whose ``state_name`` matches ``state``, walking
        back from the latest sequence. Stops at the first row whose
        ``state_name`` doesn't match.

        Rows whose ``failure_phase == 'can_run'`` are skipped (they
        neither count toward the run nor break it). Those rows record
        "the ticket was held and the state never actually executed" —
        they are not runaway-defense signal. Without this skip, holding
        a ticket for cap+ polls accumulated cap+ can_run-failed rows and
        immediately tripped the cap on operator-resume, escalating the
        resumed ticket to NeedsHelp (Phase 8d.15 Bug F4).

        Used by the state machine (Phase 8c.2) to detect a runaway loop
        on a single state before the daemon burns more cycles. Returns
        zero if no instances exist for the ticket.
        """
        ...

    # --- Dependency tracking ---

    def set_ticket_dependencies(self, ticket_id: int, *, deps: list[int]) -> None: ...
    def get_ticket_dependencies(self, ticket_id: int) -> list[int]: ...
    def list_unmet_dependencies(self, ticket_id: int) -> list[int]: ...


_TERMINAL_STATES = frozenset({"Done", "Failed"})


class InMemoryTicketRepository:
    """Reference TicketRepository for unit tests.

    Behavior must match SqliteTicketRepository — the same test suite runs
    against both. If you find a behavior gap, the bug is in whichever impl
    diverges from the test.

    Not thread-safe; single-threaded test use only.
    """

    def __init__(self) -> None:
        self._tickets: dict[int, TicketRecord] = {}
        self._by_issue: dict[tuple[str, int], int] = {}
        self._instances: dict[int, StateInstanceRecord] = {}
        self._next_ticket_id = 1
        self._next_instance_id = 1

    # --- Ticket CRUD ---

    def create_ticket(self, *, project: str, issue_number: int, now: dt.datetime) -> TicketRecord:
        if (project, issue_number) in self._by_issue:
            raise TicketAlreadyExistsError(f"{project}#{issue_number}")
        ticket = TicketRecord(
            id=self._next_ticket_id,
            project=project,
            issue_number=issue_number,
            current_state="Queued",
            created_at=now,
            updated_at=now,
            held_by=None,
            held_at=None,
            held_reason=None,
        )
        self._tickets[ticket.id] = ticket
        self._by_issue[(project, issue_number)] = ticket.id
        self._next_ticket_id += 1
        return ticket

    def get_ticket(self, ticket_id: int) -> TicketRecord:
        try:
            return self._tickets[ticket_id]
        except KeyError as exc:
            raise TicketNotFoundError(str(ticket_id)) from exc

    def get_ticket_by_issue(self, *, project: str, issue_number: int) -> TicketRecord:
        try:
            return self._tickets[self._by_issue[(project, issue_number)]]
        except KeyError as exc:
            raise TicketNotFoundError(f"{project}#{issue_number}") from exc

    def list_open_tickets(self) -> list[TicketRecord]:
        return [t for t in self._tickets.values() if t.current_state not in _TERMINAL_STATES]

    def list_all_tickets(self) -> list[TicketRecord]:
        return list(self._tickets.values())

    def set_ticket_state(self, ticket_id: int, new_state: str, *, now: dt.datetime) -> None:
        existing = self.get_ticket(ticket_id)
        self._tickets[ticket_id] = dataclasses.replace(
            existing, current_state=new_state, updated_at=now
        )

    def hold_ticket(self, ticket_id: int, *, held_by: str, reason: str, now: dt.datetime) -> None:
        existing = self.get_ticket(ticket_id)
        self._tickets[ticket_id] = dataclasses.replace(
            existing,
            held_by=held_by,
            held_at=now,
            held_reason=reason,
            updated_at=now,
        )

    def resume_ticket(self, ticket_id: int, *, now: dt.datetime) -> None:
        existing = self.get_ticket(ticket_id)
        self._tickets[ticket_id] = dataclasses.replace(
            existing,
            held_by=None,
            held_at=None,
            held_reason=None,
            updated_at=now,
        )

    # --- State-instance journal ---

    def open_state_instance(
        self, *, ticket_id: int, state_name: str, sequence: int, now: dt.datetime
    ) -> StateInstanceRecord:
        self.get_ticket(ticket_id)  # raise if missing
        instance = StateInstanceRecord(
            id=self._next_instance_id,
            ticket_id=ticket_id,
            state_name=state_name,
            sequence=sequence,
            entered_at=now,
            execute_started_at=None,
            execute_completed_at=None,
            exited_at=None,
            outcome_kind=None,
            outcome_payload=None,
            next_state=None,
            failure_phase=None,
            failure_reason=None,
        )
        self._instances[instance.id] = instance
        self._next_instance_id += 1
        return instance

    def get_state_instance(self, instance_id: int) -> StateInstanceRecord:
        try:
            return self._instances[instance_id]
        except KeyError as exc:
            raise StateInstanceNotFoundError(str(instance_id)) from exc

    def _replace(self, instance_id: int, **changes: Any) -> None:
        existing = self.get_state_instance(instance_id)
        self._instances[instance_id] = dataclasses.replace(existing, **changes)

    def mark_execute_started(self, instance_id: int, *, now: dt.datetime) -> None:
        self._replace(instance_id, execute_started_at=now)

    def mark_execute_completed(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        outcome_kind: OutcomeKind,
        outcome_payload: dict[str, Any],
        next_state: str,
    ) -> None:
        self._replace(
            instance_id,
            execute_completed_at=now,
            outcome_kind=outcome_kind,
            outcome_payload=outcome_payload,
            next_state=next_state,
        )

    def close_state_instance(self, instance_id: int, *, now: dt.datetime) -> None:
        self._replace(instance_id, exited_at=now)

    def record_failure(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        failure_phase: str,
        failure_reason: str,
    ) -> None:
        self._replace(
            instance_id,
            failure_phase=failure_phase,
            failure_reason=failure_reason,
        )

    def list_in_flight_state_instances(self) -> list[StateInstanceRecord]:
        return [i for i in self._instances.values() if i.is_in_flight]

    def list_state_instances_for_ticket(
        self, ticket_id: int,
    ) -> list[StateInstanceRecord]:
        matches = [
            i for i in self._instances.values() if i.ticket_id == ticket_id
        ]
        matches.sort(key=lambda i: i.sequence)
        return matches

    # --- Helpers used by states / WorkerPool / QueueManager ---

    def latest_pr_number_for_ticket(self, ticket_id: int) -> int | None:
        candidates = [
            i for i in self._instances.values() if i.ticket_id == ticket_id
        ]
        candidates.sort(key=lambda i: i.sequence, reverse=True)
        for inst in candidates:
            if not inst.outcome_payload:
                continue
            pr_number = (inst.outcome_payload or {}).get("artifacts", {}).get("pr_number")
            if pr_number is not None:
                return int(pr_number)
        return None

    def count_state_instances_for_ticket(self, ticket_id: int) -> int:
        return sum(1 for i in self._instances.values() if i.ticket_id == ticket_id)

    def count_consecutive_same_state(
        self, *, ticket_id: int, state: str
    ) -> int:
        matches = [
            i for i in self._instances.values() if i.ticket_id == ticket_id
        ]
        matches.sort(key=lambda i: i.sequence, reverse=True)
        count = 0
        for inst in matches:
            # Phase 8d.15 (F4): can_run-failed rows record "the ticket
            # was held and the state never executed" — they are not
            # runaway-defense signal. Skip them entirely (neither count
            # nor break the run) so a held-then-resumed ticket doesn't
            # immediately escalate to NeedsHelp.
            if inst.failure_phase == "can_run":
                continue
            if inst.state_name == state:
                count += 1
            else:
                break
        return count

    # --- Dependency tracking ---

    def set_ticket_dependencies(self, ticket_id: int, *, deps: list[int]) -> None:
        existing = self.get_ticket(ticket_id)
        self._tickets[ticket_id] = dataclasses.replace(existing, depends_on=list(deps))

    def get_ticket_dependencies(self, ticket_id: int) -> list[int]:
        return list(self.get_ticket(ticket_id).depends_on)

    def list_unmet_dependencies(self, ticket_id: int) -> list[int]:
        deps = self.get_ticket_dependencies(ticket_id)
        return [d for d in deps if self.get_ticket(d).current_state != "Done"]
