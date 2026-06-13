"""Lifecycle events — the notification stream observers consume.

These events are emitted by TicketState.transition() at each hook boundary.
They are pure data — observers decide what (if anything) to do with them.
Events MUST NOT carry references to live objects (repos, sockets); only
serializable values. This keeps audit / replay implementations honest.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from foreman.v4.outcome import Outcome


@dataclass(frozen=True, slots=True)
class Event:
    """Common fields for every lifecycle event."""
    ticket_id: int
    instance_id: int
    state_name: str
    sequence: int
    at: dt.datetime


@dataclass(frozen=True, slots=True)
class StateEnteredEvent(Event):
    """``enter()`` returned successfully."""


@dataclass(frozen=True, slots=True)
class ExecuteStartedEvent(Event):
    """``execute()`` is about to be called."""


@dataclass(frozen=True, slots=True)
class ExecuteCompletedEvent(Event):
    """``execute()`` returned an Outcome and ``verify()`` passed."""
    outcome: Outcome
    next_state: str


@dataclass(frozen=True, slots=True)
class StateExitedEvent(Event):
    """``exit()`` returned. ``outcome`` is None if execute() raised."""
    outcome: Outcome | None


@dataclass(frozen=True, slots=True)
class StateFailedEvent(Event):
    """A lifecycle hook raised. ``failure_phase`` matches the failed hook."""
    failure_phase: str
    failure_reason: str
