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

from foreman.v4.dependency_reconciler import compute_unmet_dependencies, find_cycles
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
    """Sweeps GitHub + the ticket repository once per tick to enqueue work.

    Owns exactly one project. A Daemon constructs one Poller per project
    (see ``bootstrap_cli_context``) and injects its single shared
    QueueManager into each of them via ``qm``, so per-project sweeps feed
    one global work queue.
    """

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
        """Run one full sweep: adopt newly-labeled issues, then re-enqueue open tickets.

        Raises:
            RuntimeError: if no QueueManager has been wired in yet (via the
                constructor or a Daemon), since there would be nothing to
                enqueue into.
        """
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
            project=self._project,
            label=self._trigger_label,
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
            self._qm.enqueue(
                WorkItem(ticket_id=ticket.id, state_name="Queued", project=ticket.project)
            )

    def _enqueue_open_tickets(self) -> None:
        assert self._qm is not None  # narrowed by tick()
        now = self._clock()

        # Collect tickets eligible for reconcile (non-terminal, not time-suspended).
        # We need the full list twice: once to reconcile deps, once to build the
        # edge map for cycle detection.  Materialise it so the two passes share
        # the same snapshot.
        open_tickets = [
            t
            for t in self._repo.list_open_tickets()
            if t.current_state not in _TERMINAL_STATES
            and not (t.next_action_at is not None and t.next_action_at > now)
        ]

        # -- Pass 1: reconcile blocked_by → depends_on for every eligible ticket. --
        # Task 4 (foreman#524): compute_unmet_dependencies reads GitHub's native
        # blocked_by list and filters to deps not yet closed-as-completed.
        # Full-replacing depends_on every tick converges to truth without a
        # separate "clear" step. The QueueManager gate
        # (list_unmet_dependencies → depends_on verbatim) skips tickets whose
        # deps are non-empty.
        for ticket in open_tickets:
            unmet = compute_unmet_dependencies(
                project=ticket.project,
                issue_number=ticket.issue_number,
                provider=self._git,
            )
            self._repo.set_ticket_dependencies(ticket.id, deps=unmet)

        # -- Pass 2: cycle detection among tracked tickets (Task 6, foreman#524). --
        # Build the edge map keyed by issue_number, restricted to targets that are
        # themselves tracked (untracked deps can hold a ticket but cannot form a
        # detectable cycle here).
        tracked_issue_numbers: set[int] = {t.issue_number for t in open_tickets}
        # Re-read reconciled depends_on from the repo (set in Pass 1 above).
        issue_to_ticket_id: dict[int, int] = {t.issue_number: t.id for t in open_tickets}
        edges: dict[int, list[int]] = {
            t.issue_number: [
                dep
                for dep in self._repo.get_ticket_dependencies(t.id)
                if dep in tracked_issue_numbers
            ]
            for t in open_tickets
        }

        cycles = find_cycles(edges)
        for cycle in cycles:
            reason = "dependency cycle: " + " ↔ ".join(f"#{n}" for n in cycle)
            for issue_number in cycle:
                ticket_id = issue_to_ticket_id[issue_number]
                current = self._repo.get_ticket(ticket_id)
                # Idempotency guard: skip if already held for the same cycle reason.
                if current.held_reason is not None and current.held_reason.startswith(
                    "dependency cycle"
                ):
                    continue
                self._repo.hold_ticket(
                    ticket_id,
                    held_by="orchestrator",
                    reason=reason,
                    now=now,
                )

        # -- Pass 3: enqueue eligible tickets. --
        # Held tickets are filtered by the QueueManager (ticket.is_held check at
        # queue_manager.py:127), so cycle-held tickets are automatically gated even
        # though we enqueue the WorkItem here.  Tickets with unmet deps are filtered
        # by list_unmet_dependencies at queue_manager.py:129.
        for ticket in open_tickets:
            self._qm.enqueue(
                WorkItem(
                    ticket_id=ticket.id,
                    state_name=ticket.current_state,
                    project=ticket.project,
                )
            )
