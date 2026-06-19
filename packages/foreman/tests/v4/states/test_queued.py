"""QueuedState — the entry hop. Transitions to Planning unconditionally."""
from __future__ import annotations

import datetime as dt

from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.queued import QueuedState


def test_queued_advances_to_planning():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Queued",
        sequence=1, now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
    )
    next_state = QueuedState().transition(ctx)
    assert next_state is not None
    assert next_state.state_name == "Planning"
    assert repo.get_ticket(ticket.id).current_state == "Planning"
