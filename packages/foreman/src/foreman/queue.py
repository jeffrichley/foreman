"""In-memory de-duped ticket queue for the Foreman daemon.

Keyed by ``(project_name, issue_number)``. Re-enqueueing an already-present
ticket updates its stored Ticket (so latest labels + timestamp win).

Sorted on dequeue, not on insert — see ``DaemonQueue.dequeue`` for the
sort key. v1 is single-process, single-worker; the queue is intentionally
in-memory (audit/recovery uses SQLite).
"""

from __future__ import annotations

from foreman.config import ProjectConfig
from foreman.dispatcher import Ticket, next_action, stage_index


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

    def dequeue(self, projects: dict[str, ProjectConfig]) -> Ticket | None:
        """Return the highest-priority actionable ticket, or None.

        Sort key (descending priority): ``(-stage_index(action), last_transition_at)``
        — further-along stages win; FIFO tiebreak by oldest transition.

        Parked tickets (``next_action`` returns None) stay in the queue but
        are never returned — they wait for a label change (which arrives
        via poller diff).

        Removes the returned ticket from the queue. Caller is responsible
        for re-enqueueing via the self-notify hook if there's more work
        after the action completes.
        """
        actionable: list[tuple[int, Ticket]] = []
        for ticket in self._items.values():
            project_cfg = projects.get(ticket.project_name)
            if project_cfg is None:
                continue
            action = next_action(ticket, project_cfg)
            if action is None:
                continue
            actionable.append((stage_index(action.kind), ticket))

        if not actionable:
            return None

        actionable.sort(key=lambda pair: (-pair[0], pair[1].last_transition_at))
        chosen = actionable[0][1]
        del self._items[(chosen.project_name, chosen.issue_number)]
        return chosen
