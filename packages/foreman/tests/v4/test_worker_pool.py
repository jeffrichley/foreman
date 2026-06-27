"""WorkerPool — ThreadPoolExecutor draining QueueManager."""
from __future__ import annotations

import datetime as dt
import threading
import time

from foreman.v4.event_bus import EventBus
from foreman.v4.events import Event, StateEnteredEvent
from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.work import WorkItem
from foreman.v4.worker_pool import WorkerPool


def _canned(kind: str) -> str:
    return f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high","summary":"x"}}'


def _wait_until_idle(qm: QueueManager, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while qm.in_flight_count() > 0 and time.time() < deadline:
        time.sleep(0.01)
    if qm.in_flight_count() > 0:
        raise AssertionError(f"qm still has {qm.in_flight_count()} in flight after {timeout}s")


def test_tick_processes_one_workitem():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    qm = QueueManager(repo=repo, max_in_flight=4)
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1): _canned("clean"),
    })
    pool = WorkerPool(
        repo=repo, qm=qm, dispatcher=dispatcher,
        git=None, bus=None,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    try:
        repo.set_ticket_state(ticket.id, "Planning", now=dt.datetime(2026, 6, 13))
        qm.enqueue(WorkItem(ticket_id=ticket.id, state_name="Planning"))
        submitted = pool.tick()
        assert submitted == 1
        _wait_until_idle(qm)
        assert repo.get_ticket(ticket.id).current_state == "SpecReview"
    finally:
        pool.shutdown(wait=True)


class _CrashOnCompleteRepo:
    """Delegates to an inner repo but raises in ``mark_execute_completed``.

    Simulates a worker-thread crash *inside* transition() that escapes its
    per-phase handlers: the state_instance row is already open and the
    StateContext is built, transition()'s ``finally`` runs (closing the row),
    then the exception re-raises into the WorkerPool future. ``record_failure``
    / ``execute`` failures are caught by transition() and return None — they
    do NOT reproduce #454. A raise from ``mark_execute_completed`` does.
    """

    def __init__(self, inner: InMemoryTicketRepository) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def mark_execute_completed(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated worker-thread crash")


def test_worker_crash_parks_ticket_on_needs_help():
    """#454: a worker-future exception must not leave the ticket in a
    non-terminal state with next_action_at NULL — the Poller would re-enqueue
    it every tick forever (silent infinite loop, evidence only in logs). The
    crash handler parks it on NeedsHelp and surfaces the landing event."""
    inner = InMemoryTicketRepository()
    ticket = inner.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    inner.set_ticket_state(ticket.id, "Planning", now=dt.datetime(2026, 6, 13))
    repo = _CrashOnCompleteRepo(inner)

    events: list[Event] = []
    bus = EventBus()
    bus.subscribe(events.append)

    qm = QueueManager(repo=repo, max_in_flight=4)
    dispatcher = FakeRoleDispatcher(responses={("planner", "p", 1): _canned("clean")})
    pool = WorkerPool(
        repo=repo, qm=qm, dispatcher=dispatcher,
        git=None, bus=bus,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    try:
        qm.enqueue(WorkItem(ticket_id=ticket.id, state_name="Planning"))
        pool.tick()
        _wait_until_idle(qm)
        # Parked terminally → Poller stops re-enqueueing.
        assert inner.get_ticket(ticket.id).current_state == "NeedsHelp"
        # Surfaced → LabelObservabilityObserver applies foreman:state-needshelp.
        assert any(
            isinstance(e, StateEnteredEvent) and e.state_name == "NeedsHelp"
            for e in events
        )
    finally:
        pool.shutdown(wait=True)


def test_worker_crash_before_context_built_still_parks_ticket():
    """Floor case (#454): the crash happens before the StateContext exists —
    here ``build_state`` raises on an unknown state_name. There's no instance
    to land a terminal event on, but the ticket must still be parked so the
    Poller stops re-enqueueing it."""
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(ticket.id, "Planning", now=dt.datetime(2026, 6, 13))
    qm = QueueManager(repo=repo, max_in_flight=4)
    pool = WorkerPool(
        repo=repo, qm=qm,
        dispatcher=FakeRoleDispatcher(responses={}),
        git=None, bus=None,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    try:
        qm.enqueue(WorkItem(ticket_id=ticket.id, state_name="NotARealState"))
        pool.tick()
        _wait_until_idle(qm)
        assert repo.get_ticket(ticket.id).current_state == "NeedsHelp"
    finally:
        pool.shutdown(wait=True)


def test_tick_returns_zero_when_queue_empty():
    repo = InMemoryTicketRepository()
    qm = QueueManager(repo=repo, max_in_flight=4)
    pool = WorkerPool(
        repo=repo, qm=qm,
        dispatcher=FakeRoleDispatcher(responses={}),
        git=None, bus=None,
        clock=lambda: dt.datetime(2026, 6, 13),
    )
    try:
        assert pool.tick() == 0
    finally:
        pool.shutdown(wait=True)


def test_three_tickets_dispatch_concurrently():
    """All three tickets reach SpecReview without serializing on a single thread."""
    repo = InMemoryTicketRepository()
    tids = []
    for i in (1, 2, 3):
        t = repo.create_ticket(project="p", issue_number=i, now=dt.datetime(2026, 6, 13))
        repo.set_ticket_state(t.id, "Planning", now=dt.datetime(2026, 6, 13))
        tids.append(t.id)
    # max_in_flight=3 sizes BOTH the QM cap and the thread pool — one knob.
    qm = QueueManager(repo=repo, max_in_flight=3)
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1): _canned("clean"),
        ("planner", "p", 2): _canned("clean"),
        ("planner", "p", 3): _canned("clean"),
    })
    pool = WorkerPool(
        repo=repo, qm=qm, dispatcher=dispatcher,
        git=None, bus=None,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    try:
        for tid in tids:
            qm.enqueue(WorkItem(ticket_id=tid, state_name="Planning"))
        submitted = pool.tick()
        assert submitted == 3
        _wait_until_idle(qm)
        for tid in tids:
            assert repo.get_ticket(tid).current_state == "SpecReview"
    finally:
        pool.shutdown(wait=True)


def test_same_ticket_serialized_across_concurrent_submissions():
    """Per-ticket FIFO holds even under thread pressure.

    Without a blocking dispatcher, the FakeRoleDispatcher transition
    completes in microseconds — possibly BEFORE tick()'s inner loop calls
    dequeue() again. That would let the second WorkItem (same ticket)
    become eligible and tick() would return submitted=2, even though
    only ever one was running at a time. Race-prone on Windows under
    load.

    The invariant is per-ticket FIFO: at most one transition for a
    given ticket is in-flight at any moment. Hold the first transition
    open with an Event so we can observe in_flight_count == 1 and
    submitted == 1 deterministically.
    """
    repo = InMemoryTicketRepository()
    t = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(t.id, "Planning", now=dt.datetime(2026, 6, 13))
    qm = QueueManager(repo=repo, max_in_flight=4)

    release = threading.Event()
    base = FakeRoleDispatcher(responses={("planner", "p", 1): _canned("clean")})

    class BlockingDispatcher:
        def dispatch(self, **kwargs):
            release.wait(timeout=5.0)
            return base.dispatch(**kwargs)

    dispatcher = BlockingDispatcher()
    pool = WorkerPool(
        repo=repo, qm=qm, dispatcher=dispatcher,
        git=None, bus=None,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    try:
        # Enqueue Planning AND SpecReview for the same ticket. QM should only
        # dispatch one at a time; the SpecReview WorkItem waits.
        qm.enqueue(WorkItem(ticket_id=t.id, state_name="Planning"))
        qm.enqueue(WorkItem(ticket_id=t.id, state_name="SpecReview"))
        submitted = pool.tick()
        # Only ONE in flight per ticket — second waits behind the per-ticket
        # FIFO filter inside QM.dequeue().
        assert submitted == 1
        assert qm.in_flight_count() == 1
        # Release the first transition; mark_done fires; second WorkItem
        # becomes eligible the next time something dequeues. We don't drive
        # tick() again here — we just verify the worker drains cleanly.
        release.set()
        _wait_until_idle(qm)
    finally:
        # Ensure cleanup even if an assertion above failed mid-flight.
        release.set()
        pool.shutdown(wait=True)
