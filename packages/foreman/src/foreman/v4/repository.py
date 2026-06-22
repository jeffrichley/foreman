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

    def create_ticket(
        self, *, project: str, issue_number: int, now: dt.datetime
    ) -> TicketRecord: ...
    def get_ticket(self, ticket_id: int) -> TicketRecord: ...
    def get_ticket_by_issue(self, *, project: str, issue_number: int) -> TicketRecord: ...
    def list_open_tickets(self) -> list[TicketRecord]: ...
    def list_all_tickets(self) -> list[TicketRecord]: ...
    def set_ticket_state(self, ticket_id: int, new_state: str, *, now: dt.datetime) -> None: ...
    def hold_ticket(
        self, ticket_id: int, *, held_by: str, reason: str, now: dt.datetime
    ) -> None: ...
    def resume_ticket(self, ticket_id: int, *, now: dt.datetime) -> None: ...
    def delete_ticket(self, ticket_id: int) -> None: ...
    # foreman#361: schedule + clear the transient-provider-error
    # suspension window. Poller filters on these.
    def set_next_action_at(self, ticket_id: int, *, when: dt.datetime) -> None: ...
    def clear_next_action_at(self, ticket_id: int) -> None: ...

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
        self,
        ticket_id: int,
    ) -> list[StateInstanceRecord]: ...
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
    ) -> None: ...
    def list_events_for_ticket(self, ticket_id: int) -> list[dict[str, Any]]: ...

    # --- Helpers used by states / WorkerPool / QueueManager ---

    def latest_pr_number_for_ticket(self, ticket_id: int) -> int | None: ...
    def count_state_instances_for_ticket(self, ticket_id: int) -> int: ...
    def count_consecutive_transient_provider_errors(self, ticket_id: int) -> int:
        """Returns the count of consecutive completed-and-recorded
        ``TRANSIENT_PROVIDER_ERROR`` outcomes for ``ticket_id``,
        excluding any in-flight attempt (rows with
        ``outcome_kind IS NULL``).

        When called from inside ``RoleDispatchState.next_state``
        (state.py:313, BEFORE ``mark_execute_completed`` at
        state.py:314), the most-recent row is the in-flight attempt
        — skipped — so the returned count is the number of PRIOR
        completed transient attempts. The caller passes that count
        directly to ``next_retry_delay(attempt)``: 0 → 30s, 1 → 2m,
        2 → 10m, 3 → 30m, 4 → None (escalate to NeedsHelp).

        When called from ``cmd_show`` AFTER
        ``mark_execute_completed`` has written the outcome (the
        suspension is already in effect by the time an operator runs
        ``foreman show <id>``), the most-recent row is a completed
        transient — counted — so the returned count is the number of
        transient attempts already observed; the ``cmd_show``
        ``attempt N/4`` display therefore matches the user-visible
        "attempt N just observed" rather than the next attempt's
        index.

        This single docstring sentence is load-bearing: it is what
        reconciles the two call sites that otherwise look
        superficially in tension.

        Skip rules: rows whose ``failure_phase == 'can_run'`` are
        skipped (same precedent as
        :meth:`count_consecutive_same_state`). Rows whose
        ``outcome_kind IS NULL`` are skipped (the in-flight row
        opened by the WorkerPool before ``transition()`` runs).
        Any non-NULL, non-can_run ``outcome_kind`` value that is
        not ``TRANSIENT_PROVIDER_ERROR.value`` breaks the run.
        """
        ...

    def count_consecutive_same_state(self, *, ticket_id: int, state: str) -> int:
        """Return the number of consecutive ``state_instances`` rows for
        ``ticket_id`` whose ``state_name`` matches ``state``, walking
        back from the latest sequence. Stops at the first row whose
        ``state_name`` doesn't match.

        Two row kinds are skipped (they neither count toward the run nor
        break it):

        * Rows whose ``failure_phase == 'can_run'`` (Phase 8d.15 Bug F4):
          these record "the ticket was held and the state never actually
          executed" — they are not runaway-defense signal. Without this
          skip, holding a ticket for cap+ polls accumulated cap+
          can_run-failed rows and immediately tripped the cap on
          operator-resume, escalating the resumed ticket to NeedsHelp.

        * Rows whose ``outcome_kind == 'blocked'`` (Phase 8d.18): these
          record a legitimate async-polling self-loop (e.g. MergingState
          polling a pending merge verdict; ImplementingState polling
          impl-PR CI). The state RAN, emitted "still waiting", and
          asked to be re-tried via ``next_state()`` returning a fresh
          instance of itself. Without this skip, 3 polling cycles trip
          the default cap and escalate to NeedsHelp — defeating the
          polling intent.

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
        self._events: list[dict[str, Any]] = []

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

    def set_next_action_at(self, ticket_id: int, *, when: dt.datetime) -> None:
        existing = self.get_ticket(ticket_id)
        self._tickets[ticket_id] = dataclasses.replace(
            existing,
            next_action_at=when,
            updated_at=when,
        )

    def clear_next_action_at(self, ticket_id: int) -> None:
        existing = self.get_ticket(ticket_id)
        if existing.next_action_at is None:
            return
        # Use the existing updated_at; in-memory has no notion of
        # "now" without a clock parameter and SqliteTicketRepository's
        # variant stamps updated_at via _to_iso(dt.datetime.now). Keep
        # the two impls behavior-equivalent by mirroring the same
        # updated_at semantics in the SQL impl (it passes
        # ``dt.datetime.now(dt.UTC)`` only because it has to give SQL a
        # value). For the in-memory impl, keeping updated_at unchanged
        # avoids spurious clock injection in repository tests; the
        # SQL impl matches the contract because callers always go
        # through it with explicit clock-based timestamps elsewhere.
        self._tickets[ticket_id] = dataclasses.replace(
            existing,
            next_action_at=None,
        )

    def delete_ticket(self, ticket_id: int) -> None:
        existing = self.get_ticket(ticket_id)  # raises TicketNotFoundError
        del self._tickets[ticket_id]
        del self._by_issue[(existing.project, existing.issue_number)]
        # Cascade — drop every state-instance row tied to this ticket.
        self._instances = {
            iid: inst for iid, inst in self._instances.items() if inst.ticket_id != ticket_id
        }

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
        self,
        ticket_id: int,
    ) -> list[StateInstanceRecord]:
        matches = [i for i in self._instances.values() if i.ticket_id == ticket_id]
        matches.sort(key=lambda i: i.sequence)
        return matches

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
        self._events.append(
            {
                "ticket_id": ticket_id,
                "instance_id": instance_id,
                "event_type": event_type,
                "state_name": state_name,
                "sequence": sequence,
                "at": at,
                "payload": dict(payload),
            }
        )

    def list_events_for_ticket(self, ticket_id: int) -> list[dict[str, Any]]:
        return [e for e in self._events if e["ticket_id"] == ticket_id]

    # --- Helpers used by states / WorkerPool / QueueManager ---

    def latest_pr_number_for_ticket(self, ticket_id: int) -> int | None:
        candidates = [i for i in self._instances.values() if i.ticket_id == ticket_id]
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

    def count_consecutive_same_state(self, *, ticket_id: int, state: str) -> int:
        matches = [i for i in self._instances.values() if i.ticket_id == ticket_id]
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
            # Phase 8d.18: BLOCKED-outcome rows record a legitimate
            # async-polling self-loop (MergingState polling a pending
            # merge verdict; ImplementingState polling impl-PR CI). The
            # state ran, emitted "still waiting", and asked to be re-
            # tried via next_state() → self. They are not runaway-
            # defense signal. Skip them entirely so a few polling cycles
            # don't trip the cap and escalate the ticket to NeedsHelp.
            if inst.outcome_kind == OutcomeKind.BLOCKED:
                continue
            # foreman#361: TRANSIENT_PROVIDER_ERROR rows record an
            # Anthropic-side blip that the RoleDispatchState handles
            # via the backoff scheduler — transient-provider self-loops
            # are not runaway-defense signal. Skip them entirely so a
            # short Anthropic outage doesn't trip the cap and escalate
            # the ticket to NeedsHelp by the wrong path.
            if inst.outcome_kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR:
                continue
            if inst.state_name == state:
                count += 1
            else:
                break
        return count

    def count_consecutive_transient_provider_errors(self, ticket_id: int) -> int:
        matches = [i for i in self._instances.values() if i.ticket_id == ticket_id]
        matches.sort(key=lambda i: i.sequence, reverse=True)
        count = 0
        for inst in matches:
            # foreman#361: same precedent as
            # count_consecutive_same_state — can_run failures don't
            # count and don't break.
            if inst.failure_phase == "can_run":
                continue
            # foreman#361 CRITICAL: skip the in-flight row.
            # ``RoleDispatchState.next_state`` is called BEFORE
            # ``mark_execute_completed`` writes the outcome_kind, so
            # the most-recent row has outcome_kind=None when we walk
            # from inside the Template Method. Without this skip,
            # every call would see "current row, NULL outcome, break"
            # and return 0 — the backoff would never advance past
            # 30s.
            if inst.outcome_kind is None:
                continue
            if inst.outcome_kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR:
                count += 1
            else:
                # Any other completed outcome breaks the run.
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
