"""LabelObservabilityObserver — stamps the current state on the issue.

Write-only by design: the daemon never reads labels back to decide state.
Labels exist for humans viewing the GitHub issue page.

The observer adds ``foreman:state-<new_state>`` when the state is
entered and removes ``foreman:state-<old_state>`` when the prior state
is exited. The two operations are intentionally split across
``StateEnteredEvent`` and ``StateExitedEvent`` so the observer stays
stateless (no per-ticket "last seen state" cache) and the event schema
stays unchanged.

The label-mutation surface is injected as a ``LabelWriter`` Protocol so
this module doesn't depend on PyGithub at test time. Production wiring
passes a GitProvider straight through — its ``add_labels`` /
``remove_labels`` shape satisfies the writer Protocol.

Why granular (not replace) writes
---------------------------------
Phase 8.1 used PyGithub's ``set_labels`` which REPLACES the entire
label set on the issue. That stripped the trigger label
(``foreman:plan``) every time the daemon stamped a state-progress
label — wedging the morning dogfood for ~40 minutes when the daemon
restarted, since the Poller couldn't re-find the ticket without its
trigger label. Phase 8c.4 switched to ``add_labels`` /
``remove_labels`` so the trigger label and any operator-applied labels
survive every state transition.

The observer deliberately does not catch writer exceptions — the
EventBus owns the per-observer firewall. Letting failures propagate
keeps this module thin and ensures bus-level isolation is what's
actually exercised under load.
"""

from __future__ import annotations

from typing import Protocol

from foreman.v4.events import Event, StateEnteredEvent, StateExitedEvent
from foreman.v4.repository import TicketRepository


class LabelWriter(Protocol):
    """Decouples observer from PyGithub. Implementations add or remove
    the given labels on the named issue. Reads are out of scope — this
    observer never inspects existing labels."""

    def add_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None: ...

    def remove_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None: ...


def _state_label(state_name: str) -> str:
    """Return the canonical ``foreman:state-<lowercased>`` label."""
    return f"foreman:state-{state_name.lower()}"


class LabelObservabilityObserver:
    """Stamps state-progress labels on the issue without touching others."""

    def __init__(self, *, writer: LabelWriter, repo: TicketRepository) -> None:
        self._writer = writer
        self._repo = repo

    def __call__(self, event: Event) -> None:
        if isinstance(event, StateEnteredEvent):
            self._on_state_entered(event)
            return
        if isinstance(event, StateExitedEvent):
            self._on_state_exited(event)
            return

    def _on_state_entered(self, event: StateEnteredEvent) -> None:
        """Add the new state's label. Never touches other labels."""
        ticket = self._repo.get_ticket(event.ticket_id)
        self._writer.add_labels(
            project=ticket.project,
            issue_number=ticket.issue_number,
            labels={_state_label(event.state_name)},
        )

    def _on_state_exited(self, event: StateExitedEvent) -> None:
        """Remove the now-old state's label.

        Pairs with ``_on_state_entered`` so a Planning → SpecReview
        transition ends with ``foreman:state-specreview`` present and
        ``foreman:state-planning`` removed — and the trigger label
        + any operator-applied labels untouched throughout. Idempotent
        on the writer side: if the label was never stamped (e.g. the
        observer joined mid-transition), the writer silently no-ops.
        """
        ticket = self._repo.get_ticket(event.ticket_id)
        self._writer.remove_labels(
            project=ticket.project,
            issue_number=ticket.issue_number,
            labels={_state_label(event.state_name)},
        )
