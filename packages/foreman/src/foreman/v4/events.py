"""Lifecycle events — the notification stream observers consume.

These events are emitted by TicketState.transition() at each hook boundary
AND (issue #360) by the daemon-level :class:`BackupScheduler` for backup
take / failure outcomes. They are pure data — observers decide what (if
anything) to do with them. Events MUST NOT carry references to live
objects (repos, sockets); only serializable values. This keeps audit /
replay implementations honest.

Type hierarchy (issue #360):

    Event                 - common base; carries only ``at``
      |-- TicketEvent     - ticket-scoped fields (id / instance /
      |     |               state_name / sequence); parent of every
      |     |               state-machine event
      |     |-- StateEnteredEvent
      |     |-- ExecuteStartedEvent
      |     |-- ExecuteCompletedEvent
      |     |-- StateExitedEvent
      |     `-- StateFailedEvent
      `-- DaemonEvent     - daemon-level fields (just ``at`` today;
            |               no ticket scope); parent of every event
            |               emitted by the daemon itself
            |-- BackupTakenEvent
            `-- BackupFailedEvent

Observers that need to read ticket-scoped fields ``isinstance``-guard
on :class:`TicketEvent` (`StructuredLogObserver`, `MetricsObserver`,
`EventArchiveObserver`, `EventBus.publish`'s exception path); observers
that handle daemon-level events ``isinstance``-guard on
:class:`DaemonEvent`. The split lets every existing
``Event``-typed signature (``EventBus.publish``, ``EventListener``,
observer ``__call__``) accept both flavors without further widening.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from foreman.v4.outcome import Outcome


@dataclass(frozen=True, slots=True)
class Event:
    """Common fields for every lifecycle event.

    Only ``at`` is shared across ticket-scoped and daemon-level events.
    Ticket-scoped fields live on :class:`TicketEvent`; daemon-level
    events inherit from :class:`DaemonEvent` and carry no ticket
    fields at all.
    """

    at: dt.datetime


@dataclass(frozen=True, slots=True)
class TicketEvent(Event):
    """Common fields for every ticket-scoped (state-machine) event.

    Every state-machine event the substrate emits has these fields:
    which ticket, which state-instance, what state, which sequence
    number within that ticket's history.
    """

    ticket_id: int
    instance_id: int
    state_name: str
    sequence: int


@dataclass(frozen=True, slots=True)
class DaemonEvent(Event):
    """Common base for every daemon-level (non-ticket-scoped) event.

    Daemon-level events describe things the daemon itself did rather
    than a state-machine transition — backup take/fail being the
    first such event family (issue #360). They MUST NOT carry
    ``ticket_id`` / ``instance_id`` / ``state_name`` / ``sequence``;
    consumers that need those fields ``isinstance``-guard on
    :class:`TicketEvent`.
    """


@dataclass(frozen=True, slots=True)
class StateEnteredEvent(TicketEvent):
    """``enter()`` returned successfully."""


@dataclass(frozen=True, slots=True)
class ExecuteStartedEvent(TicketEvent):
    """``execute()`` is about to be called."""


@dataclass(frozen=True, slots=True)
class ExecuteCompletedEvent(TicketEvent):
    """``execute()`` returned an Outcome and ``verify()`` passed."""
    outcome: Outcome
    next_state: str


@dataclass(frozen=True, slots=True)
class StateExitedEvent(TicketEvent):
    """``exit()`` returned. ``outcome`` is None if execute() raised."""
    outcome: Outcome | None


@dataclass(frozen=True, slots=True)
class StateFailedEvent(TicketEvent):
    """A lifecycle hook raised. ``failure_phase`` matches the failed hook."""
    failure_phase: str
    failure_reason: str


@dataclass(frozen=True, slots=True)
class BackupTakenEvent(DaemonEvent):
    """The :class:`BackupScheduler` took a snapshot successfully.

    ``path`` is the on-disk snapshot path as a string (NOT a
    ``Path`` object) so the event payload stays serializable / portable
    across observer write paths. ``pruned_count`` is the number of
    old snapshot files the post-snapshot pruner deleted on this tick.
    """

    path: str
    size_bytes: int
    pruned_count: int


@dataclass(frozen=True, slots=True)
class BackupFailedEvent(DaemonEvent):
    """The :class:`BackupScheduler` failed to take or prune a snapshot.

    ``phase`` is ``"snapshot"`` when the failure surfaced from
    :func:`take_snapshot` and ``"prune"`` when from
    :func:`prune_snapshots`. ``reason`` is ``str(exc)`` truncated to
    500 chars to keep the structured-log line tractable.
    """

    phase: Literal["snapshot", "prune"]
    reason: str
