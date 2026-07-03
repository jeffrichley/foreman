"""WorkItem — the v4 queue contract.

A WorkItem is just "advance ticket T from state S to whatever next_state
returns." Hashable so the QueueManager can dedup.

``project`` is required so ``QueueManager.mark_done`` can decrement the
per-project in-flight counter (issue #472) without a repository round-trip.
The Poller always has the ``TicketRecord`` (which carries ``project``) at
WorkItem construction time.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One unit of queueable work: advance a ticket from its current state.

    Frozen + slotted so instances are hashable — the QueueManager dedups
    pending work by WorkItem equality, so re-enqueuing the same
    (ticket, state) pair on a later Poller tick is a no-op.
    """

    ticket_id: int
    state_name: str
    project: str
