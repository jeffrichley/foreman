"""QueueManager — priority heap + multi-filter dequeue.

Producer/consumer Mediator between Poller and WorkerPool. The queue is a
priority heap keyed by (state's distance to Done, enqueue sequence) so
late-stage work drains before early-stage work fills the pipeline.

Four filters apply at dequeue time, in order:

  1. ticket already in flight (per-ticket FIFO serialization)
  2. ticket held by an operator
  3. ticket has unmet dependencies
  4. project per-concurrency cap exceeded (issue #472)

A filtered WorkItem stays in the heap; it's not requeued, not reordered.
The next dequeue() re-evaluates everyone naturally.

Threading: instance methods take an internal lock; safe for the
ThreadPoolExecutor-based WorkerPool (Task 4.4) to call concurrently.
"""

from __future__ import annotations

import heapq
import itertools
import threading

from foreman.v4.repository import TicketRepository
from foreman.v4.work import WorkItem

_STATE_PRIORITY = {
    "Merging":      1,
    # foreman#416: SpecMerging is a fast non-role merge state (like
    # Merging) that should drain promptly so the spec PR lands and the
    # build can start — same top priority tier as Merging.
    "SpecMerging":  1,
    "ImplReview":   2,
    "Implementing": 3,
    "ImplFix":      3,
    "SpecReview":   4,
    "SpecFix":      4,
    "Planning":     5,
    "Queued":       6,
}
_DEFAULT_PRIORITY = 99


def _priority_for(state_name: str) -> int:
    return _STATE_PRIORITY.get(state_name, _DEFAULT_PRIORITY)


class QueueManager:
    """Priority-heap work queue shared by every Poller and drained by the WorkerPool.

    Wraps a heap keyed by (state-priority, enqueue sequence) with the four
    dequeue-time filters described in the module docstring (in-flight,
    held, unmet dependencies, per-project cap). All public methods take
    the internal lock, so Poller producers and the WorkerPool consumer
    may call them concurrently.
    """

    def __init__(
        self,
        *,
        repo: TicketRepository,
        max_in_flight: int,
        project_caps: dict[str, int | None] | None = None,
    ) -> None:
        self._repo = repo
        # Public: WorkerPool reads this to size its ThreadPoolExecutor.
        # The contract is "the single concurrency knob"; treat as read-only
        # after construction.
        self.max_in_flight = max_in_flight
        # issue #472: per-project concurrency cap. None means unbounded (only
        # the global max_in_flight applies). Coerce None to empty dict so
        # every lookup is a simple dict.get() without a None-guard.
        self._project_caps: dict[str, int | None] = project_caps or {}
        self._heap: list[tuple[int, int, WorkItem]] = []
        self._counter = itertools.count()  # tie-breaker = enqueue order
        self._queued: set[WorkItem] = set()
        self._in_flight: set[WorkItem] = set()
        self._in_flight_tickets: set[int] = set()
        # issue #472: per-project in-flight counter. Guarded by the same
        # RLock as _in_flight_tickets so both stay in sync.
        self._in_flight_by_project: dict[str, int] = {}
        self._lock = threading.RLock()  # RLock so future observers can reenter QM safely

    def enqueue(self, item: WorkItem) -> None:
        """Push ``item`` onto the heap unless it's already queued or in flight.

        A duplicate of a pending or currently-executing item is silently
        dropped rather than re-queued or re-prioritized.
        """
        with self._lock:
            if item in self._queued or item in self._in_flight:
                return
            heapq.heappush(
                self._heap,
                (_priority_for(item.state_name), next(self._counter), item),
            )
            self._queued.add(item)

    def dequeue(self) -> WorkItem | None:
        """Pop the highest-priority eligible WorkItem, or return None if none qualifies.

        Applies the four filters (per-ticket FIFO serialization, operator
        hold, unmet dependencies, per-project cap) in order. A filtered
        candidate is popped off the heap and pushed back in the
        ``finally`` clause rather than requeued or reordered, so the next
        call re-evaluates it from scratch. Returns None immediately if
        the global ``max_in_flight`` cap is already saturated.
        """
        with self._lock:
            if len(self._in_flight_tickets) >= self.max_in_flight:
                return None
            skipped: list[tuple[int, int, WorkItem]] = []
            try:
                while self._heap:
                    entry = heapq.heappop(self._heap)
                    # Claim the entry now so any exception below leaves it in
                    # skipped (and therefore re-pushed by the finally clause).
                    # On the success path we pop it back off before dispatch.
                    skipped.append(entry)
                    _, _, candidate = entry
                    if candidate.ticket_id in self._in_flight_tickets:
                        continue  # per-ticket FIFO — wait for prior
                    ticket = self._repo.get_ticket(candidate.ticket_id)
                    if ticket.is_held:
                        continue
                    if self._repo.list_unmet_dependencies(candidate.ticket_id):
                        continue
                    # issue #472: per-project cap filter. A candidate whose
                    # project is at its cap is skipped (not None-returned) so
                    # a ticket from a different project may still be eligible.
                    cap = self._project_caps.get(ticket.project)
                    if cap is not None and self._in_flight_by_project.get(ticket.project, 0) >= cap:
                        continue
                    skipped.pop()  # un-claim on success — entry won't be re-pushed
                    self._queued.discard(candidate)
                    self._in_flight.add(candidate)
                    self._in_flight_tickets.add(candidate.ticket_id)
                    # issue #472: increment per-project counter on dequeue.
                    self._in_flight_by_project[ticket.project] = (
                        self._in_flight_by_project.get(ticket.project, 0) + 1
                    )
                    return candidate
                return None
            finally:
                for entry in skipped:
                    heapq.heappush(self._heap, entry)

    def mark_done(self, item: WorkItem) -> None:
        """Idempotent: no-op if item was not in flight."""
        with self._lock:
            self._in_flight.discard(item)
            self._in_flight_tickets.discard(item.ticket_id)
            # issue #472: decrement per-project counter. Guard against going
            # negative (defensive — mark_done is declared idempotent so a
            # double-call must not corrupt the counter).
            count = self._in_flight_by_project.get(item.project, 0)
            if count > 0:
                self._in_flight_by_project[item.project] = count - 1

    def in_flight_count(self) -> int:
        """Return how many WorkItems are currently dequeued and executing."""
        with self._lock:
            return len(self._in_flight_tickets)

    def queue_depth(self) -> int:
        """Return how many WorkItems are waiting in the heap, not yet dequeued."""
        with self._lock:
            return len(self._heap)
