"""In-memory de-duped ticket queue for the Foreman daemon.

Keyed by ``(project_name, issue_number)``. Re-enqueueing an already-present
ticket updates its stored Ticket (so latest labels + timestamp win).

Sorted on dequeue, not on insert — see ``DaemonQueue.dequeue`` for the
sort key. v1 is single-process, single-worker; the queue is intentionally
in-memory (audit/recovery uses SQLite).
"""

from __future__ import annotations

from foreman.dispatcher import Ticket


class DaemonQueue:
    """De-duped FIFO of pending tickets.

    De-duped by ``(project_name, issue_number)``. A second enqueue of the
    same ticket overwrites the stored Ticket with the newer one — fresher
    labels win over stale, which matters when poller and self-notify race.
    """

    def __init__(self) -> None:
        self._items: dict[tuple[str, int], Ticket] = {}

    def enqueue(self, ticket: Ticket) -> None:
        """Insert or replace the ticket."""
        self._items[(ticket.project_name, ticket.issue_number)] = ticket

    def __len__(self) -> int:
        return len(self._items)

    def snapshot(self) -> list[Ticket]:
        """Read-only copy of current queue contents."""
        return list(self._items.values())
