"""MergeQueuedState — parked hand-off state (foreman#550).

A ticket lands here after Merging/SpecMerging enqueue its PR on the
project's merge_queue. From here the (forthcoming) MergeCoordinator drives
the ticket, not the WorkerPool — QueueManager.dequeue excludes MergeQueued
tickets (see test_queue_manager.py). These tests only cover this state in
isolation: its name, registry round-trip, and the defensive self-loop
shape in case it's ever dispatched by mistake.
"""

from __future__ import annotations

import datetime as dt

from foreman.v4.outcome import OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.merge_queued import MergeQueuedState
from foreman.v4.states.registry import STATE_REGISTRY, build_state


def _ctx() -> StateContext:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 7, 17))
    repo.set_ticket_state(ticket.id, "MergeQueued", now=dt.datetime(2026, 7, 17))
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="MergeQueued",
        sequence=1,
        now=dt.datetime(2026, 7, 17),
    )
    return StateContext(
        ticket=repo.get_ticket(ticket.id),
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 7, 17),
    )


def test_merge_queued_state_name():
    assert MergeQueuedState.state_name == "MergeQueued"


def test_registry_round_trips_merge_queued():
    assert STATE_REGISTRY["MergeQueued"] is MergeQueuedState
    assert isinstance(build_state("MergeQueued"), MergeQueuedState)


def test_merge_queued_execute_returns_blocked_defensively():
    """MergeQueued should never actually be dispatched by the WorkerPool
    (the QueueManager excludes it) — but if it ever is, execute() re-parks
    with BLOCKED instead of raising or silently advancing."""
    ctx = _ctx()
    outcome = MergeQueuedState().execute(ctx)
    assert outcome.kind == OutcomeKind.BLOCKED


def test_merge_queued_next_state_self_loops():
    ctx = _ctx()
    outcome = MergeQueuedState().execute(ctx)
    next_state = MergeQueuedState().next_state(ctx, outcome)
    assert isinstance(next_state, MergeQueuedState)


def test_merge_queued_transition_self_loops():
    ctx = _ctx()
    next_state = MergeQueuedState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "MergeQueued"
