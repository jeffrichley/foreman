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
    ticket_id: int
    state_name: str
    project: str
