"""Poller — the only producer of WorkItems in v4.

One ``tick()`` does a full sweep:

  1. Newly-labeled GitHub issues → create ticket rows, enqueue Queued.
  2. Open tickets in non-terminal states → enqueue current_state.
     The QueueManager dedups by WorkItem, so repeated ticks don't duplicate.

The Poller intentionally does NOT track per-tick "what changed since last
tick" state. The QueueManager + journal handle that dedup naturally:
re-enqueuing the same WorkItem is a no-op (QueueManager dedup), and the
WorkerPool's transition() always opens a new state_instance row, so even
if it runs the same logical state twice in a row, the journal stays
linear and the role's idempotency takes care of any visible-to-GitHub
duplication (deferred per spec C3).

A real daemon calls tick() on a cadence; tests call it manually.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from foreman.v4.git_provider import GitProvider
from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import TicketAlreadyExistsError, TicketRepository
from foreman.v4.work import WorkItem

# foreman#443: ``ImplApproved`` was removed — it polls each tick to
# detect the human merge, so the Poller must re-enqueue it (same as
# any other non-terminal state). Only the final resolved terminals
# (Done/Failed/NeedsHelp) are skipped here.
_TERMINAL_STATES = frozenset({"Done", "Failed", "NeedsHelp"})


class Poller:
    def __init__(
        self,
        *,
        repo: TicketRepository,
        qm: QueueManager | None,
        git: GitProvider,
        project: str,
        trigger_label: str,
        clock: Callable[[], dt.datetime],
    ) -> None:
        # qm may be None at construction time; Daemon's `_with_qm` helper
        # injects the shared QueueManager when the Daemon is built. This
        # lets callers construct Pollers per-project and let the Daemon
        # wire one shared queue across all of them.
        self._repo = repo
        self._qm = qm
        self._git = git
        self._project = project
        self._trigger_label = trigger_label
        self._clock = clock

    def tick(self) -> None:
        if self._qm is None:
            # Constructed without a QM and never wired by a Daemon — tick()
            # would have no queue to enqueue into. Fail loud so this isn't
            # mistaken for "tick ran but found nothing."
            raise RuntimeError(
                "Poller.tick() called before a QueueManager was wired. "
                "Either pass qm= at construction, or attach the Poller to "
                "a Daemon (which injects its shared QM).",
            )
        self._adopt_new_tickets()
        self._enqueue_open_tickets()

    def _adopt_new_tickets(self) -> None:
        assert self._qm is not None  # narrowed by tick()
        issue_numbers = self._git.list_open_issues_with_label(
            project=self._project, label=self._trigger_label,
        )
        for issue_number in issue_numbers:
            try:
                ticket = self._repo.create_ticket(
                    project=self._project,
                    issue_number=issue_number,
                    now=self._clock(),
                )
            except TicketAlreadyExistsError:
                continue
            self._qm.enqueue(WorkItem(ticket_id=ticket.id, state_name="Queued"))

    def _enqueue_open_tickets(self) -> None:
        assert self._qm is not None  # narrowed by tick()
        now = self._clock()
        for ticket in self._repo.list_open_tickets():
            if ticket.current_state in _TERMINAL_STATES:
                continue
            # foreman#361: respect the transient-provider-error
            # backoff suspension. Tickets whose ``next_action_at`` is
            # in the future are NOT enqueued; the next tick after
            # ``next_action_at`` re-tries them. Clearing happens
            # inside ``RoleDispatchState.next_state`` on any
            # non-transient outcome (defense in depth) and via
            # ``cmd_retry`` for operator overrides — we deliberately
            # do NOT clear here so a daemon restart mid-suspension
            # picks up the same suspension window from the repository.
            if ticket.next_action_at is not None and ticket.next_action_at > now:
                continue
            self._qm.enqueue(WorkItem(
                ticket_id=ticket.id, state_name=ticket.current_state,
            ))
