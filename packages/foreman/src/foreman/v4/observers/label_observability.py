"""LabelObservabilityObserver — writes one foreman:state-* label per state entry.

Write-only by design: the daemon never reads labels back to decide state.
Labels exist for humans viewing the GitHub issue page.

The actual label-mutation surface is injected as a ``LabelWriter`` Protocol
so this module doesn't have to know about PyGithub at test time. Production
wiring lives in Phase 5.

The observer deliberately does not catch writer exceptions — the EventBus
owns the per-observer firewall. Letting failures propagate keeps this
module thin and ensures bus-level isolation is what's actually exercised
under load.
"""

from __future__ import annotations

from typing import Protocol

from foreman.v4.events import Event, StateEnteredEvent
from foreman.v4.repository import TicketRepository


class LabelWriter(Protocol):
    """Decouples observer from PyGithub. Implementations write the given
    labels onto the named issue. Reads are out of scope — this observer
    never inspects existing labels."""

    def write_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None: ...


class LabelObservabilityObserver:
    """Reacts to ``StateEnteredEvent`` by stamping the current state on the issue."""

    def __init__(self, *, writer: LabelWriter, repo: TicketRepository) -> None:
        self._writer = writer
        self._repo = repo

    def __call__(self, event: Event) -> None:
        if not isinstance(event, StateEnteredEvent):
            return
        ticket = self._repo.get_ticket(event.ticket_id)
        label = f"foreman:state-{event.state_name.lower()}"
        self._writer.write_labels(
            project=ticket.project,
            issue_number=ticket.issue_number,
            labels={label},
        )
