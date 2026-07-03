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
    """Decouples the observer from PyGithub for testability.

    Implementations add or remove the given labels on the named issue.
    Reads are out of scope — this observer never inspects existing labels.
    """

    def add_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None:
        """Add ``labels`` to the issue, leaving any other labels untouched."""
        ...

    def remove_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None:
        """Remove ``labels`` from the issue, leaving any other labels untouched."""
        ...


def _state_label(state_name: str) -> str:
    """Return the canonical ``foreman:state-<lowercased>`` label."""
    return f"foreman:state-{state_name.lower()}"


#: Operator-applied trigger label the Poller searches for. Once the ticket
#: has progressed into a real state, the trigger has done its job and the
#: observer drops it so the issue's label set reflects current lifecycle
#: position only. Hardcoded here (mirrors ``ProjectConfig.trigger_label``
#: default) because the observer is global to the EventBus, not per-project
#: — threading per-project config through would require a project→label
#: lookup the observer doesn't otherwise need. If a project ever customizes
#: its trigger label, this constant becomes the gap to plug.
_TRIGGER_LABEL = "foreman:plan"

#: The first states a ticket enters after Poller pickup. Both are listed
#: defensively: tickets are CREATED in ``Queued``, so the normal path is
#: ``StateEnteredEvent(Queued)`` first. But if the observer joined mid-
#: transition (e.g. daemon restart after Queued already exited), the first
#: event it sees is ``StateEnteredEvent(Planning)`` and the trigger label
#: still needs to be cleared. Cheaper than tracking per-ticket "have I
#: cleared the trigger yet?" state — the writer no-ops idempotently if
#: the label is already gone.
_FIRST_STATES = frozenset({"Queued", "Planning"})


class LabelObservabilityObserver:
    """Stamps state-progress labels on the issue without touching others."""

    def __init__(self, *, writer: LabelWriter, repo: TicketRepository) -> None:
        self._writer = writer
        self._repo = repo

    def __call__(self, event: Event) -> None:
        """Route state-entered/exited events to their label-mutation handler.

        Any other event type is ignored — this observer only reacts to
        the two events that bracket a state transition.
        """
        if isinstance(event, StateEnteredEvent):
            self._on_state_entered(event)
            return
        if isinstance(event, StateExitedEvent):
            self._on_state_exited(event)
            return

    def _on_state_entered(self, event: StateEnteredEvent) -> None:
        """Add the new state's label, and on first-state entry drop the trigger label.

        Phase 8d.8 moved the ``foreman:plan`` removal here after 8d.3
        stripped the Planner's manual removal. Roles don't touch labels;
        the observer owns the entire label surface. The trigger removal
        runs on entry to ``Queued`` OR ``Planning`` — both are treated as
        "first states" so a daemon that restarts mid-transition still
        clears the trigger when it sees Planning fresh.
        """
        ticket = self._repo.get_ticket(event.ticket_id)
        self._writer.add_labels(
            project=ticket.project,
            issue_number=ticket.issue_number,
            labels={_state_label(event.state_name)},
        )
        if event.state_name in _FIRST_STATES:
            self._writer.remove_labels(
                project=ticket.project,
                issue_number=ticket.issue_number,
                labels={_TRIGGER_LABEL},
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
