"""QueueManager — priority heap + multi-filter dequeue.

Producer/consumer Mediator between Poller and WorkerPool. The queue is a
priority heap keyed by (state's distance to Done, enqueue sequence) so
late-stage work drains before early-stage work fills the pipeline.

Three filters apply at dequeue time, in order:

  1. ticket already in flight (per-ticket FIFO serialization)
  2. ticket held by an operator
  3. ticket has unmet dependencies

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
    def __init__(self, *, repo: TicketRepository, max_in_flight: int) -> None:
        self._repo = repo
        # Public: WorkerPool reads this to size its ThreadPoolExecutor.
        # The contract is "the single concurrency knob"; treat as read-only
        # after construction.
        self.max_in_flight = max_in_flight
        self._heap: list[tuple[int, int, WorkItem]] = []
        self._counter = itertools.count()  # tie-breaker = enqueue order
        self._queued: set[WorkItem] = set()
        self._in_flight: set[WorkItem] = set()
        self._in_flight_tickets: set[int] = set()
        self._lock = threading.RLock()  # RLock so future observers can reenter QM safely

    def enqueue(self, item: WorkItem) -> None:
        with self._lock:
            if item in self._queued or item in self._in_flight:
                return
            heapq.heappush(
                self._heap,
                (_priority_for(item.state_name), next(self._counter), item),
            )
            self._queued.add(item)

    def dequeue(self) -> WorkItem | None:
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
                    skipped.pop()  # un-claim on success — entry won't be re-pushed
                    self._queued.discard(candidate)
                    self._in_flight.add(candidate)
                    self._in_flight_tickets.add(candidate.ticket_id)
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

    def in_flight_count(self) -> int:
        with self._lock:
            return len(self._in_flight_tickets)

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._heap)
