"""QueueManager — priority heap + multi-filter dequeue.

Producer/consumer Mediator between Poller and WorkerPool. The queue is a
priority heap keyed by (state's distance to Done, enqueue sequence) so
late-stage work drains before early-stage work fills the pipeline.

Five filters apply at dequeue time, in order:

  1. ticket already in flight (per-ticket FIFO serialization)
  2. ticket in a state that declares ``dispatch_priority = None`` —
     coordinator-driven or terminal, never worker-driven (foreman#550)
  3. ticket held by an operator
  4. ticket has unmet dependencies
  5. project per-concurrency cap exceeded (issue #472)

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
from foreman.v4.states.registry import STATE_DISPATCH_PRIORITY
from foreman.v4.work import WorkItem

# foreman#589 draws a line between two different "no priority" cases, because
# they need opposite handling:
#
#   * A REGISTERED state that declares no priority is a CODE defect. It is
#     unrepresentable — ``states/registry.py`` raises at import, so it can
#     never reach this module. That is what killed the old ``_DEFAULT_PRIORITY
#     = 99`` sentinel, under which ImplApproved silently sorted last.
#
#   * A state name that is not in the registry at all is a DATA condition — a
#     row written by an older schema, a rename, a hand-edited DB. It must NOT
#     raise here. ``Poller._enqueue_open_tickets`` loops over every open
#     ticket, so one bad row would abort the pass and starve every ticket
#     behind it. The system already handles this correctly: the item is
#     dispatched, the worker fails to build a state for it, and the ticket is
#     parked in NeedsHelp where a human can see it (asserted by
#     ``test_worker_crash_before_context_built_still_parks_ticket``). So an
#     unknown state sorts LAST but stays dispatchable — fail-visible per
#     ticket, rather than fail-loud for the whole queue.

#: Heap sort key for a state that declares ``dispatch_priority = None``. Such
#: items are filtered at dequeue regardless; this only keeps them from sitting
#: in front of real work. It is NOT a priority anyone chose.
_UNDISPATCHED_SORT_KEY = 1 << 30

#: Heap sort key for a state name absent from the registry. Ordered behind
#: every real priority but ahead of the undispatched block, so the item still
#: reaches a worker and gets parked.
_UNKNOWN_STATE_SORT_KEY = 1 << 29


def _is_worker_dispatched(state_name: str) -> bool:
    """Report whether the WorkerPool may run ``state_name``.

    Args:
        state_name: Name of the state, as stored on the ticket.

    Returns:
        ``False`` only for a registered state that declares
        ``dispatch_priority = None`` (terminal or coordinator-driven). An
        unregistered name returns ``True`` so the worker can park the ticket.
    """
    if state_name not in STATE_DISPATCH_PRIORITY:
        return True
    return STATE_DISPATCH_PRIORITY[state_name] is not None


def _sort_key_for(state_name: str) -> int:
    """Return the heap sort key for ``state_name``.

    Args:
        state_name: Name of the state, as stored on the ticket.

    Returns:
        The state's declared priority, or one of the two trailing sentinels
        for undispatched and unregistered states respectively.
    """
    priority = STATE_DISPATCH_PRIORITY.get(state_name)
    if state_name not in STATE_DISPATCH_PRIORITY:
        return _UNKNOWN_STATE_SORT_KEY
    return _UNDISPATCHED_SORT_KEY if priority is None else priority


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
                (_sort_key_for(item.state_name), next(self._counter), item),
            )
            self._queued.add(item)

    def dequeue(self) -> WorkItem | None:
        """Pop the highest-priority eligible WorkItem, or return None if none qualifies.

        Applies the five filters (per-ticket FIFO serialization,
        MergeQueued exclusion, operator hold, unmet dependencies,
        per-project cap) in order. A filtered candidate is popped off the
        heap and pushed back in the ``finally`` clause rather than
        requeued or reordered, so the next call re-evaluates it from
        scratch. Returns None immediately if the global ``max_in_flight``
        cap is already saturated.
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
                    # foreman#550: a coordinator-driven state consumes no
                    # worker slot. Checked directly off the WorkItem (no repo
                    # round-trip needed) since state_name travels with the
                    # candidate. foreman#589 replaced a literal "MergeQueued"
                    # comparison with the declared priority, so a new
                    # undispatched state is excluded by declaring it rather
                    # than by remembering to edit this loop.
                    if not _is_worker_dispatched(candidate.state_name):
                        continue
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

    def evict_merge_queued(self, ticket_id: int) -> None:
        """Drop any queued ``MergeQueued`` WorkItem for ``ticket_id`` from the heap.

        foreman#550. A ``MergeQueued`` WorkItem is never popped by
        ``dequeue()`` (see the MergeQueued filter above) — it is always
        pushed back onto the heap so it stays available for the day the
        ticket's state changes and a future dequeue needs to re-evaluate
        it. But when the ``MergeCoordinator`` moves a ticket OUT of
        ``MergeQueued`` directly (bypassing this QueueManager entirely,
        since coordinator-driven merges never go through
        ``dequeue()``/``mark_done()``), nothing else ever removes that
        WorkItem: the Poller's next sweep enqueues a NEW WorkItem for the
        ticket's new state, but the stale ``MergeQueued`` entry is a
        DIFFERENT WorkItem (different ``state_name``, so no dedup
        collapse) and would otherwise linger in the heap forever — a
        "heap zombie". Call this once, right after routing a ticket away
        from ``MergeQueued``. No-op if no such entry exists (idempotent).
        """
        with self._lock:
            self._heap = [
                candidate
                for candidate in self._heap
                if not (
                    candidate[2].ticket_id == ticket_id and candidate[2].state_name == "MergeQueued"
                )
            ]
            heapq.heapify(self._heap)
            self._queued = {
                item
                for item in self._queued
                if not (item.ticket_id == ticket_id and item.state_name == "MergeQueued")
            }
