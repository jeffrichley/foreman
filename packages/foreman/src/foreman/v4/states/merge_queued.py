"""MergeQueuedState — parked state; the coordinator drives merges from here.

foreman#550. ``MergingState`` and ``SpecMerging`` hand a ticket off to this
state after enqueueing its PR on the project's ``merge_queue`` (via
``TicketRepository.enqueue_merge``). From here the ticket is
coordinator-driven, not worker-driven: the (forthcoming) ``MergeCoordinator``
daemon component drains ``merge_queue`` directly and moves the ticket on to
its post-merge state (``Implementing`` for a merged spec PR, ``Done`` for a
merged impl PR) or a failure route, entirely outside the
WorkerPool/TicketState lifecycle. ``QueueManager.dequeue`` excludes
``MergeQueued`` tickets so they never consume a worker slot while parked
here (see ``queue_manager.py``).

``execute()`` should never actually run in production — nothing enqueues a
``MergeQueued`` WorkItem for dispatch (the QueueManager filter sees to
that). It's implemented defensively anyway: if some future caller (a bug, a
manual CLI action, a misconfigured test) dispatches it regardless, it
re-parks itself with a BLOCKED outcome rather than raising or silently
advancing, so a stray dispatch fails safe instead of doing something
unobservable.
"""

from __future__ import annotations

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.state import StateContext, TicketState


class MergeQueuedState(TicketState):
    """Parked state: the MergeCoordinator (not the WorkerPool) drives merges from here."""

    state_name = "MergeQueued"

    def execute(self, ctx: StateContext) -> Outcome:
        """Defensive no-op: re-park with BLOCKED if ever dispatched.

        See the module docstring — this should never actually run since the
        QueueManager excludes MergeQueued tickets from dequeue.
        """
        return Outcome(
            kind=OutcomeKind.BLOCKED,
            confidence=OutcomeConfidence.HIGH,
            summary="MergeQueued is coordinator-driven; not expected to be dispatched",
        )

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        """Self-loop unconditionally — parked here until the coordinator moves the ticket elsewhere directly."""
        return MergeQueuedState()
