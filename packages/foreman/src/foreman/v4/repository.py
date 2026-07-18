"""The TicketRepository seam.

Two implementations: InMemoryTicketRepository for tests;
PostgresTicketRepository for production. Domain code talks only to the
Protocol — never to a database driver or to the dict storage directly.
This is the only place persistence concerns leak into the v4 codebase.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any, Protocol

from foreman.v4.outcome import OutcomeKind, is_runaway_exempt, is_transient_error_exempt
from foreman.v4.records import (
    StateInstanceRecord,
    TicketRecord,
)


class TicketNotFoundError(LookupError):
    """No ticket exists with the given id or (project, issue_number)."""


class StateInstanceNotFoundError(LookupError):
    """No state-instance exists with the given id."""


class TicketAlreadyExistsError(ValueError):
    """A ticket with this (project, issue_number) is already tracked."""


class MissingPRNumberError(LookupError):
    """No state outcome on this ticket recorded a pr_number."""


@dataclasses.dataclass(frozen=True, slots=True)
class MergeQueueEntry:
    """Read-only snapshot of one merge_queue row — a PR waiting to merge.

    Written by the per-repo :class:`~foreman.v4.merge_coordinator.MergeCoordinator`
    (a later task) to serialize merges into a project's target branch:
    only one entry per project is ever ``merging`` at a time, and entries
    are drained strictly FIFO by ``enqueued_at``.
    """

    id: int
    project: str
    ticket_id: int
    pr_number: int
    kind: str  # "spec" | "impl"
    status: str  # "queued" | "merging"
    attempts: int
    enqueued_at: dt.datetime


class TicketRepository(Protocol):
    """Persistence contract for tickets and state-instances."""

    # --- Ticket CRUD ---

    def create_ticket(self, *, project: str, issue_number: int, now: dt.datetime) -> TicketRecord:
        """Create and persist a new ticket in the Queued state.

        Raises:
            TicketAlreadyExistsError: ``(project, issue_number)`` is already
                tracked.
        """
        ...

    def get_ticket(self, ticket_id: int) -> TicketRecord:
        """Fetch a ticket by id, raising TicketNotFoundError if absent."""
        ...

    def get_ticket_by_issue(self, *, project: str, issue_number: int) -> TicketRecord:
        """Fetch a ticket by its ``(project, issue_number)`` natural key.

        Raises:
            TicketNotFoundError: no ticket is tracked for this key.
        """
        ...

    def list_open_tickets(self) -> list[TicketRecord]:
        """Return every ticket not in a terminal state (Done or Failed)."""
        ...

    def list_all_tickets(self) -> list[TicketRecord]:
        """Return every tracked ticket, including terminal ones."""
        ...

    def set_ticket_state(self, ticket_id: int, new_state: str, *, now: dt.datetime) -> None:
        """Transition a ticket to ``new_state`` and stamp ``updated_at``."""
        ...

    def hold_ticket(self, ticket_id: int, *, held_by: str, reason: str, now: dt.datetime) -> None:
        """Mark a ticket held, blocking further state transitions until resumed.

        Records who requested the hold and why so operators and the
        contract suite can audit it later.
        """
        ...

    def resume_ticket(self, ticket_id: int, *, now: dt.datetime) -> None:
        """Clear a ticket's held status so state transitions can proceed again."""
        ...

    def delete_ticket(self, ticket_id: int) -> None:
        """Permanently remove a ticket, cascading to its state-instance history."""
        ...

    # foreman#361: schedule + clear the transient-provider-error
    # suspension window. Poller filters on these.
    def set_next_action_at(self, ticket_id: int, *, when: dt.datetime) -> None:
        """Suspend a ticket from dispatch until ``when``.

        Used to back off after a transient provider error; the Poller
        filters out tickets whose ``next_action_at`` is still in the future.
        """
        ...

    def clear_next_action_at(self, ticket_id: int) -> None:
        """Clear a ticket's dispatch suspension so it is immediately eligible again."""
        ...

    # --- State-instance journal ---

    def open_state_instance(
        self, *, ticket_id: int, state_name: str, sequence: int, now: dt.datetime
    ) -> StateInstanceRecord:
        """Start a new state-instance journal row for ``ticket_id``.

        Raises:
            TicketNotFoundError: ``ticket_id`` does not exist.
        """
        ...

    def get_state_instance(self, instance_id: int) -> StateInstanceRecord:
        """Fetch a state-instance by id, raising StateInstanceNotFoundError if absent."""
        ...

    def mark_execute_started(self, instance_id: int, *, now: dt.datetime) -> None:
        """Record the timestamp at which ``execute()`` began running."""
        ...

    def mark_execute_completed(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        outcome_kind: OutcomeKind,
        outcome_payload: dict[str, Any],
        next_state: str,
    ) -> None:
        """Record the Outcome ``execute()`` produced and when it completed.

        This is the row the runaway-defense and transient-error counters
        (below) walk back over.
        """
        ...

    def close_state_instance(self, instance_id: int, *, now: dt.datetime) -> None:
        """Record the timestamp ``exit()`` finished, closing the instance's lifecycle."""
        ...

    def record_failure(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        failure_phase: str,
        failure_reason: str,
    ) -> None:
        """Record which lifecycle phase failed and why for a state-instance."""
        ...

    def set_session_id(self, instance_id: int, session_id: str) -> None:
        """Attach a role subprocess's Claude session id to a state-instance.

        Enables resuming that session on a subsequent retry of the same
        state instance instead of starting a fresh one.
        """
        ...

    def list_in_flight_state_instances(self) -> list[StateInstanceRecord]:
        """Return every state-instance, across all tickets, not yet closed.

        Used at startup to find crash orphans (rows a killed daemon never
        got to call ``close_state_instance`` on).
        """
        ...

    def list_state_instances_for_ticket(
        self,
        ticket_id: int,
    ) -> list[StateInstanceRecord]:
        """Return every state-instance recorded for a ticket, ordered by sequence."""
        ...

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
        """Append an audit-log row for a ticket/state-instance occurrence."""
        ...

    def list_events_for_ticket(self, ticket_id: int) -> list[dict[str, Any]]:
        """Return every recorded event for a ticket, in insertion order."""
        ...

    # --- Helpers used by states / WorkerPool / QueueManager ---

    def latest_pr_number_for_ticket(self, ticket_id: int) -> int | None:
        """Return the most recent ``pr_number`` recorded for a ticket.

        Scans the ticket's state-instances newest-first for an
        ``outcome_payload["artifacts"]["pr_number"]``; returns None if no
        instance ever recorded one.
        """
        ...

    def count_state_instances_for_ticket(self, ticket_id: int) -> int:
        """Return how many state-instances have been opened for this ticket."""
        ...

    def count_consecutive_transient_provider_errors(self, ticket_id: int) -> int:
        """Return the count of consecutive completed transient-provider-error outcomes.

        Counts completed-and-recorded ``TRANSIENT_PROVIDER_ERROR`` outcomes
        for ``ticket_id``, excluding any in-flight attempt (rows with
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
        """Return the number of consecutive ``state_instances`` rows in the same state.

        Counts rows for ``ticket_id`` whose ``state_name`` matches
        ``state``, walking back from the latest sequence. Stops at the
        first row whose ``state_name`` doesn't match.

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

    def set_ticket_dependencies(self, ticket_id: int, *, deps: list[int]) -> None:
        """Replace a ticket's full set of dependency ticket ids with ``deps``."""
        ...

    def get_ticket_dependencies(self, ticket_id: int) -> list[int]:
        """Return the ticket ids this ticket is recorded as depending on."""
        ...

    def list_unmet_dependencies(self, ticket_id: int) -> list[int]:
        """Return the stored ``depends_on`` list verbatim.

        The reconciler (Task 4) guarantees that ``depends_on`` holds only
        currently-unmet dep issue numbers before this method is called.
        This method does **no** Done-state filtering — it simply returns
        what is stored so the QueueManager gate works purely on the
        reconciler-maintained unmet-dep set.

        Unlike the old implementation, this does **not** call
        ``get_ticket(dep)`` for each dep, so it is safe when ``depends_on``
        contains issue numbers of untracked GitHub issues.
        """
        ...

    # --- Merge queue (foreman#550) ---

    def enqueue_merge(
        self, *, project: str, ticket_id: int, pr_number: int, kind: str, now: dt.datetime
    ) -> MergeQueueEntry:
        """Append a new ``queued`` entry to ``project``'s merge queue.

        ``kind`` is ``"spec"`` or ``"impl"``. FIFO ordering is by
        ``now`` (stamped as ``enqueued_at``), so callers must pass a
        monotonically increasing clock for entries meant to serialize
        in submission order.
        """
        ...

    def merge_queue_for_project(self, project: str) -> list[MergeQueueEntry]:
        """Return every entry for ``project``, ordered ascending by ``enqueued_at`` (FIFO)."""
        ...

    def head_merge_entry(self, project: str) -> MergeQueueEntry | None:
        """Return ``project``'s earliest ``queued`` or ``merging`` entry, or None if empty."""
        ...

    def mark_merge_active(self, entry_id: int) -> None:
        """Transition the entry's status to ``"merging"``."""
        ...

    def increment_merge_attempts(self, entry_id: int) -> int:
        """Increment the entry's ``attempts`` counter by one and return the new count."""
        ...

    def dequeue_merge(self, entry_id: int) -> None:
        """Permanently remove the entry from the merge queue."""
        ...

    def list_active_merges(self) -> list[MergeQueueEntry]:
        """Return every entry, across all projects, whose status is ``"merging"``.

        Used at startup for crash recovery — a killed daemon may have
        left an entry marked ``merging`` mid-attempt.
        """
        ...


_TERMINAL_STATES = frozenset({"Done", "Failed"})


class InMemoryTicketRepository:
    """Reference TicketRepository for unit tests.

    Behavior must match the production :class:`PostgresTicketRepository` —
    the shared :class:`TicketRepository` contract suite runs against both.
    If you find a behavior gap, the bug is in whichever impl diverges from
    the contract.

    Not thread-safe; single-threaded test use only.
    """

    def __init__(self) -> None:
        self._tickets: dict[int, TicketRecord] = {}
        self._by_issue: dict[tuple[str, int], int] = {}
        self._instances: dict[int, StateInstanceRecord] = {}
        self._next_ticket_id = 1
        self._next_instance_id = 1
        self._events: list[dict[str, Any]] = []
        self._merge_queue: list[MergeQueueEntry] = []
        self._next_merge_entry_id = 1

    # --- Ticket CRUD ---

    def create_ticket(self, *, project: str, issue_number: int, now: dt.datetime) -> TicketRecord:
        """Allocate the next ticket id and store a new ticket in the Queued state.

        Raises:
            TicketAlreadyExistsError: ``(project, issue_number)`` is already
                tracked.
        """
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
        """Look up a ticket by id.

        Raises:
            TicketNotFoundError: no ticket with ``ticket_id`` exists.
        """
        try:
            return self._tickets[ticket_id]
        except KeyError as exc:
            raise TicketNotFoundError(str(ticket_id)) from exc

    def get_ticket_by_issue(self, *, project: str, issue_number: int) -> TicketRecord:
        """Look up a ticket via the ``(project, issue_number)`` index.

        Raises:
            TicketNotFoundError: no ticket is tracked for this key.
        """
        try:
            return self._tickets[self._by_issue[(project, issue_number)]]
        except KeyError as exc:
            raise TicketNotFoundError(f"{project}#{issue_number}") from exc

    def list_open_tickets(self) -> list[TicketRecord]:
        """Return every ticket whose ``current_state`` isn't Done or Failed."""
        return [t for t in self._tickets.values() if t.current_state not in _TERMINAL_STATES]

    def list_all_tickets(self) -> list[TicketRecord]:
        """Return every tracked ticket, in dict-insertion order."""
        return list(self._tickets.values())

    def set_ticket_state(self, ticket_id: int, new_state: str, *, now: dt.datetime) -> None:
        """Replace the ticket's ``current_state`` and ``updated_at``."""
        existing = self.get_ticket(ticket_id)
        self._tickets[ticket_id] = dataclasses.replace(
            existing, current_state=new_state, updated_at=now
        )

    def hold_ticket(self, ticket_id: int, *, held_by: str, reason: str, now: dt.datetime) -> None:
        """Stamp the ticket as held by ``held_by`` for ``reason``."""
        existing = self.get_ticket(ticket_id)
        self._tickets[ticket_id] = dataclasses.replace(
            existing,
            held_by=held_by,
            held_at=now,
            held_reason=reason,
            updated_at=now,
        )

    def resume_ticket(self, ticket_id: int, *, now: dt.datetime) -> None:
        """Clear the ticket's held_by/held_at/held_reason fields."""
        existing = self.get_ticket(ticket_id)
        self._tickets[ticket_id] = dataclasses.replace(
            existing,
            held_by=None,
            held_at=None,
            held_reason=None,
            updated_at=now,
        )

    def set_next_action_at(self, ticket_id: int, *, when: dt.datetime) -> None:
        """Stamp the ticket's ``next_action_at`` suspension deadline."""
        existing = self.get_ticket(ticket_id)
        self._tickets[ticket_id] = dataclasses.replace(
            existing,
            next_action_at=when,
            updated_at=when,
        )

    def clear_next_action_at(self, ticket_id: int) -> None:
        """Clear a ticket's ``next_action_at`` suspension if one is set.

        No-op if already clear. Deliberately leaves ``updated_at``
        unchanged (see comment below) rather than requiring a clock
        argument this method doesn't take, so the two implementations
        stay behavior-equivalent per the contract suite.
        """
        existing = self.get_ticket(ticket_id)
        if existing.next_action_at is None:
            return
        # Use the existing updated_at; in-memory has no notion of
        # "now" without a clock parameter and the production
        # PostgresTicketRepository stamps updated_at with an explicit
        # timestamp. Keep the two impls behavior-equivalent: for the
        # in-memory impl, keeping updated_at unchanged avoids spurious
        # clock injection in repository tests; the Postgres impl matches
        # the contract because callers always go through it with explicit
        # clock-based timestamps elsewhere.
        self._tickets[ticket_id] = dataclasses.replace(
            existing,
            next_action_at=None,
        )

    def delete_ticket(self, ticket_id: int) -> None:
        """Remove the ticket and its issue index entry, cascading to its journal.

        Every state-instance row tied to ``ticket_id`` is dropped along
        with the ticket itself.

        Raises:
            TicketNotFoundError: no ticket with ``ticket_id`` exists.
        """
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
        """Verify the ticket exists, then create and store a new state-instance row.

        The new row's lifecycle fields (``execute_started_at``,
        ``outcome_kind``, etc.) all start empty; subsequent ``mark_*``
        and ``record_failure`` calls fill them in as the state runs.

        Raises:
            TicketNotFoundError: ``ticket_id`` does not exist.
        """
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
        """Look up a state-instance by id.

        Raises:
            StateInstanceNotFoundError: no instance with ``instance_id``
                exists.
        """
        try:
            return self._instances[instance_id]
        except KeyError as exc:
            raise StateInstanceNotFoundError(str(instance_id)) from exc

    def _replace(self, instance_id: int, **changes: Any) -> None:
        existing = self.get_state_instance(instance_id)
        self._instances[instance_id] = dataclasses.replace(existing, **changes)

    def mark_execute_started(self, instance_id: int, *, now: dt.datetime) -> None:
        """Stamp ``execute_started_at`` on the instance."""
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
        """Stamp the instance with the Outcome execute() produced and its completion time."""
        self._replace(
            instance_id,
            execute_completed_at=now,
            outcome_kind=outcome_kind,
            outcome_payload=outcome_payload,
            next_state=next_state,
        )

    def close_state_instance(self, instance_id: int, *, now: dt.datetime) -> None:
        """Stamp ``exited_at``, closing the instance's lifecycle."""
        self._replace(instance_id, exited_at=now)

    def record_failure(
        self,
        instance_id: int,
        *,
        now: dt.datetime,
        failure_phase: str,
        failure_reason: str,
    ) -> None:
        """Stamp ``failure_phase`` and ``failure_reason`` on the instance."""
        self._replace(
            instance_id,
            failure_phase=failure_phase,
            failure_reason=failure_reason,
        )

    def set_session_id(self, instance_id: int, session_id: str) -> None:
        """Stamp the role subprocess's Claude session id on the instance."""
        self._replace(instance_id, session_id=session_id)

    def list_in_flight_state_instances(self) -> list[StateInstanceRecord]:
        """Return every instance, across all tickets, whose ``exited_at`` is still None."""
        return [i for i in self._instances.values() if i.is_in_flight]

    def list_state_instances_for_ticket(
        self,
        ticket_id: int,
    ) -> list[StateInstanceRecord]:
        """Return every instance for the ticket, sorted ascending by sequence."""
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
        """Append an event row to the in-memory log.

        ``payload`` is defensively copied so later caller-side mutation
        of the dict they passed in can't retroactively change the logged
        event.
        """
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
        """Return every logged event whose ``ticket_id`` matches, in append order."""
        return [e for e in self._events if e["ticket_id"] == ticket_id]

    # --- Helpers used by states / WorkerPool / QueueManager ---

    def latest_pr_number_for_ticket(self, ticket_id: int) -> int | None:
        """Return the most recent ``pr_number`` recorded for a ticket, or None.

        Scans the ticket's instances newest-first (by sequence) and
        returns the first ``outcome_payload["artifacts"]["pr_number"]``
        found; skips instances with no recorded payload.
        """
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
        """Return how many instances belong to the ticket."""
        return sum(1 for i in self._instances.values() if i.ticket_id == ticket_id)

    def count_consecutive_same_state(self, *, ticket_id: int, state: str) -> int:
        """Count consecutive newest-first instances of the ticket still in ``state``.

        Walks the ticket's instances from the latest sequence backward,
        skipping rows :func:`~foreman.v4.outcome.is_runaway_exempt` marks
        as not runaway-defense signal, and stops counting at the first
        non-exempt row whose ``state_name`` doesn't match ``state``.
        """
        matches = [i for i in self._instances.values() if i.ticket_id == ticket_id]
        matches.sort(key=lambda i: i.sequence, reverse=True)
        count = 0
        for inst in matches:
            if is_runaway_exempt(inst.failure_phase, inst.outcome_kind):
                continue
            if inst.state_name == state:
                count += 1
            else:
                break
        return count

    def count_consecutive_transient_provider_errors(self, ticket_id: int) -> int:
        """Count consecutive newest-first transient-provider-error outcomes.

        Walks the ticket's instances from the latest sequence backward,
        skipping rows :func:`~foreman.v4.outcome.is_transient_error_exempt`
        marks as exempt, and stops counting at the first non-exempt row
        whose outcome isn't ``TRANSIENT_PROVIDER_ERROR``.
        """
        matches = [i for i in self._instances.values() if i.ticket_id == ticket_id]
        matches.sort(key=lambda i: i.sequence, reverse=True)
        count = 0
        for inst in matches:
            if is_transient_error_exempt(inst.failure_phase, inst.outcome_kind):
                continue
            if inst.outcome_kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR:
                count += 1
            else:
                # Any other completed outcome breaks the run.
                break
        return count

    # --- Dependency tracking ---

    def set_ticket_dependencies(self, ticket_id: int, *, deps: list[int]) -> None:
        """Replace the ticket's ``depends_on`` list with ``deps``."""
        existing = self.get_ticket(ticket_id)
        self._tickets[ticket_id] = dataclasses.replace(existing, depends_on=list(deps))

    def get_ticket_dependencies(self, ticket_id: int) -> list[int]:
        """Return a copy of the ticket's recorded dependency ticket ids."""
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

    def _replace_merge_entry(self, entry_id: int, **changes: Any) -> MergeQueueEntry:
        for i, existing in enumerate(self._merge_queue):
            if existing.id == entry_id:
                updated = dataclasses.replace(existing, **changes)
                self._merge_queue[i] = updated
                return updated
        raise LookupError(str(entry_id))

    def enqueue_merge(
        self, *, project: str, ticket_id: int, pr_number: int, kind: str, now: dt.datetime
    ) -> MergeQueueEntry:
        """Append a new ``queued`` entry, allocating the next merge-queue id."""
        entry = MergeQueueEntry(
            id=self._next_merge_entry_id,
            project=project,
            ticket_id=ticket_id,
            pr_number=pr_number,
            kind=kind,
            status="queued",
            attempts=0,
            enqueued_at=now,
        )
        self._merge_queue.append(entry)
        self._next_merge_entry_id += 1
        return entry

    def merge_queue_for_project(self, project: str) -> list[MergeQueueEntry]:
        """Return the project's entries sorted ascending by ``enqueued_at`` (FIFO)."""
        matches = [e for e in self._merge_queue if e.project == project]
        matches.sort(key=lambda e: e.enqueued_at)
        return matches

    def head_merge_entry(self, project: str) -> MergeQueueEntry | None:
        """Return the project's earliest entry, or None if its queue is empty."""
        entries = self.merge_queue_for_project(project)
        return entries[0] if entries else None

    def mark_merge_active(self, entry_id: int) -> None:
        """Stamp the entry's ``status`` as ``"merging"``."""
        self._replace_merge_entry(entry_id, status="merging")

    def increment_merge_attempts(self, entry_id: int) -> int:
        """Increment the entry's ``attempts`` counter and return the new count."""
        for existing in self._merge_queue:
            if existing.id == entry_id:
                updated = self._replace_merge_entry(entry_id, attempts=existing.attempts + 1)
                return updated.attempts
        raise LookupError(str(entry_id))

    def dequeue_merge(self, entry_id: int) -> None:
        """Remove the entry from the in-memory queue."""
        self._merge_queue = [e for e in self._merge_queue if e.id != entry_id]

    def list_active_merges(self) -> list[MergeQueueEntry]:
        """Return every entry, across all projects, whose status is ``"merging"``."""
        return [e for e in self._merge_queue if e.status == "merging"]
