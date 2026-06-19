"""WorkItem — the v4 queue contract.

A WorkItem is just "advance ticket T from state S to whatever next_state
returns." Two fields. Hashable so the QueueManager can dedup.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkItem:
    ticket_id: int
    state_name: str
