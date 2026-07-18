"""QueueManager — priority heap + multi-filter dequeue."""

from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.work import WorkItem


@pytest.fixture()
def repo() -> InMemoryTicketRepository:
    return InMemoryTicketRepository()


def _ticket(repo, issue_number: int, state: str = "Planning", project: str = "p") -> int:
    t = repo.create_ticket(project=project, issue_number=issue_number, now=dt.datetime(2026, 6, 13))
    repo.set_ticket_state(t.id, state, now=dt.datetime(2026, 6, 13))
    return t.id


def test_enqueue_then_dequeue_returns_same_item(repo):
    tid = _ticket(repo, 1)
    qm = QueueManager(repo=repo, max_in_flight=4)
    item = WorkItem(ticket_id=tid, state_name="Planning", project="p")
    qm.enqueue(item)
    assert qm.dequeue() == item


def test_dedup_collapses_repeated_enqueue(repo):
    tid = _ticket(repo, 1)
    qm = QueueManager(repo=repo, max_in_flight=4)
    item = WorkItem(ticket_id=tid, state_name="Planning", project="p")
    qm.enqueue(item)
    qm.enqueue(item)
    qm.enqueue(item)
    assert qm.dequeue() == item
    assert qm.dequeue() is None


def test_empty_queue_returns_none(repo):
    qm = QueueManager(repo=repo, max_in_flight=4)
    assert qm.dequeue() is None


def test_priority_late_stage_dequeued_before_early(repo):
    """A Merging ticket comes out before a Queued ticket, regardless of enqueue order."""
    early = _ticket(repo, 1, state="Queued")
    late = _ticket(repo, 2, state="Merging")
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=early, state_name="Queued", project="p"))
    qm.enqueue(WorkItem(ticket_id=late, state_name="Merging", project="p"))
    first = qm.dequeue()
    assert first is not None and first.ticket_id == late  # Merging has priority 1, beats Queued's 6


def test_priority_tie_breaker_is_enqueue_order(repo):
    """Two same-priority WorkItems dequeue FIFO."""
    a = _ticket(repo, 1, state="Planning")
    b = _ticket(repo, 2, state="Planning")
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=a, state_name="Planning", project="p"))
    qm.enqueue(WorkItem(ticket_id=b, state_name="Planning", project="p"))
    assert qm.dequeue().ticket_id == a
    assert qm.dequeue().ticket_id == b


def test_per_ticket_in_flight_serialization(repo):
    """At most one transition per ticket runs at a time."""
    tid = _ticket(repo, 1)
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=tid, state_name="Planning", project="p"))
    # Even if Poller re-enqueues the same ticket at a different state mid-transition,
    # the QM holds it until the first WorkItem completes.
    first = qm.dequeue()
    qm.enqueue(WorkItem(ticket_id=tid, state_name="SpecReview", project="p"))
    assert qm.dequeue() is None  # ticket already in flight
    qm.mark_done(first)
    second = qm.dequeue()
    assert second is not None and second.ticket_id == tid


def test_global_max_in_flight_cap(repo):
    a = _ticket(repo, 1)
    b = _ticket(repo, 2)
    c = _ticket(repo, 3)
    qm = QueueManager(repo=repo, max_in_flight=2)
    qm.enqueue(WorkItem(ticket_id=a, state_name="Planning", project="p"))
    qm.enqueue(WorkItem(ticket_id=b, state_name="Planning", project="p"))
    qm.enqueue(WorkItem(ticket_id=c, state_name="Planning", project="p"))
    qm.dequeue()
    qm.dequeue()
    assert qm.dequeue() is None  # at cap
    assert qm.in_flight_count() == 2


def test_merge_queued_ticket_is_never_dequeued(repo):
    """foreman#550: a ticket parked in MergeQueued is coordinator-driven —
    the QueueManager must never hand it to the WorkerPool, even when a
    slot is free. It stays in the heap (not dropped), same as the
    held/dep-blocked filters."""
    tid = _ticket(repo, 1, state="MergeQueued")
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=tid, state_name="MergeQueued", project="p"))
    assert qm.dequeue() is None
    assert qm.queue_depth() == 1


def test_mark_done_frees_slot_for_other_ticket(repo):
    a = _ticket(repo, 1)
    b = _ticket(repo, 2)
    qm = QueueManager(repo=repo, max_in_flight=1)
    qm.enqueue(WorkItem(ticket_id=a, state_name="Planning", project="p"))
    qm.enqueue(WorkItem(ticket_id=b, state_name="Planning", project="p"))
    first = qm.dequeue()
    qm.mark_done(first)
    second = qm.dequeue()
    assert second is not None and second.ticket_id == b


def test_held_ticket_is_skipped_and_stays_in_heap(repo):
    held = _ticket(repo, 1)
    other = _ticket(repo, 2)
    repo.hold_ticket(held, held_by="jeff", reason="x", now=dt.datetime(2026, 6, 13))
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=held, state_name="Planning", project="p"))
    qm.enqueue(WorkItem(ticket_id=other, state_name="Planning", project="p"))
    item = qm.dequeue()
    assert item is not None and item.ticket_id == other
    # Resume → the held one becomes eligible without re-enqueue
    qm.mark_done(item)
    repo.resume_ticket(held, now=dt.datetime(2026, 6, 13))
    item = qm.dequeue()
    assert item is not None and item.ticket_id == held


def test_dep_blocked_ticket_is_skipped_until_upstream_done(repo):
    upstream = _ticket(repo, 1, state="Implementing")
    downstream = _ticket(repo, 2, state="Planning")
    repo.set_ticket_dependencies(downstream, deps=[upstream])
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=upstream, state_name="Implementing", project="p"))
    qm.enqueue(WorkItem(ticket_id=downstream, state_name="Planning", project="p"))
    # Implementing has priority 3 (lower number = higher priority) vs Planning's 5;
    # upstream comes out first. Downstream is dep-blocked anyway.
    first = qm.dequeue()
    assert first is not None and first.ticket_id == upstream
    assert qm.dequeue() is None  # downstream blocked by deps
    qm.mark_done(first)
    # foreman#524: depends_on now holds only *currently-unmet* deps and is owned
    # by the poll-time reconciler (which clears a dep once its upstream issue is
    # closed-as-completed). The queue gate itself checks depends_on emptiness, not
    # upstream ticket state — so simulate the reconciler clearing the resolved dep.
    repo.set_ticket_dependencies(downstream, deps=[])
    second = qm.dequeue()
    assert second is not None and second.ticket_id == downstream


def test_repo_exception_leaves_entry_in_heap(repo):
    """If the repo raises mid-dequeue, the popped entry must not be lost."""
    tid = _ticket(repo, 1)
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=tid, state_name="Planning", project="p"))

    # Patch get_ticket to raise on first call, succeed after
    original_get_ticket = repo.get_ticket
    call_count = {"n": 0}

    def flaky_get_ticket(ticket_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated transient repo error")
        return original_get_ticket(ticket_id)

    repo.get_ticket = flaky_get_ticket  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="simulated"):
        qm.dequeue()

    # Entry must still be in heap; subsequent dequeue must succeed
    assert qm.queue_depth() == 1
    item = qm.dequeue()
    assert item is not None and item.ticket_id == tid


def test_held_and_dep_blocked_stays_in_heap_until_both_clear(repo):
    upstream = _ticket(repo, 1, state="Implementing")
    target = _ticket(repo, 2, state="Planning")
    repo.set_ticket_dependencies(target, deps=[upstream])
    repo.hold_ticket(target, held_by="jeff", reason="x", now=dt.datetime(2026, 6, 13))
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=upstream, state_name="Implementing", project="p"))
    qm.enqueue(WorkItem(ticket_id=target, state_name="Planning", project="p"))
    # Upstream dispatches; target is filtered by BOTH held and unmet-deps
    first = qm.dequeue()
    assert first is not None and first.ticket_id == upstream
    assert qm.dequeue() is None
    qm.mark_done(first)
    # Release hold — still dep-blocked
    repo.resume_ticket(target, now=dt.datetime(2026, 6, 13))
    assert qm.dequeue() is None
    # Resolve dep — now eligible. foreman#524: depends_on is the reconciler-owned
    # unmet set; the queue gate checks its emptiness, not upstream ticket state, so
    # simulate the reconciler clearing the resolved dep.
    repo.set_ticket_dependencies(target, deps=[])
    second = qm.dequeue()
    assert second is not None and second.ticket_id == target


def test_in_flight_count_and_queue_depth(repo):
    a = _ticket(repo, 1)
    b = _ticket(repo, 2)
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=a, state_name="Planning", project="p"))
    qm.enqueue(WorkItem(ticket_id=b, state_name="Planning", project="p"))
    assert qm.queue_depth() == 2
    qm.dequeue()
    qm.dequeue()
    assert qm.in_flight_count() == 2
    assert qm.queue_depth() == 0


# ---------------------------------------------------------------------------
# issue #472: per-project max_in_flight cap
# ---------------------------------------------------------------------------


def test_per_project_cap_limits_single_project(repo):
    """Project capped at 1 yields at most 1 dequeued ticket even at global cap 4."""
    a = _ticket(repo, 1, project="fast")
    b = _ticket(repo, 2, project="fast")
    qm = QueueManager(repo=repo, max_in_flight=4, project_caps={"fast": 1})
    qm.enqueue(WorkItem(ticket_id=a, state_name="Planning", project="fast"))
    qm.enqueue(WorkItem(ticket_id=b, state_name="Planning", project="fast"))
    first = qm.dequeue()
    assert first is not None
    assert qm.dequeue() is None  # "fast" at its cap


def test_per_project_cap_does_not_block_other_projects(repo):
    """Two projects capped at 1 with global cap 2: one from each runs concurrently."""
    a = _ticket(repo, 1, project="alpha")
    b = _ticket(repo, 2, project="beta")
    qm = QueueManager(
        repo=repo,
        max_in_flight=2,
        project_caps={"alpha": 1, "beta": 1},
    )
    qm.enqueue(WorkItem(ticket_id=a, state_name="Planning", project="alpha"))
    qm.enqueue(WorkItem(ticket_id=b, state_name="Planning", project="beta"))
    first = qm.dequeue()
    second = qm.dequeue()
    assert first is not None
    assert second is not None
    assert {first.project, second.project} == {"alpha", "beta"}


def test_evict_merge_queued_drops_the_stale_entry(repo):
    """foreman#550: MergeCoordinator's heap-zombie cleanup — once a ticket
    leaves MergeQueued, the stale WorkItem the Poller enqueued while it sat
    parked must be dropped entirely, not just filtered."""
    tid = _ticket(repo, 1, state="MergeQueued")
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=tid, state_name="MergeQueued", project="p"))
    assert qm.queue_depth() == 1

    qm.evict_merge_queued(tid)

    assert qm.queue_depth() == 0
    assert qm.dequeue() is None


def test_evict_merge_queued_only_targets_that_ticket_and_state(repo):
    """Eviction is scoped to (ticket_id, "MergeQueued") -- other queued
    WorkItems, including a different state for the SAME ticket, survive."""
    tid = _ticket(repo, 1, state="Planning")
    other = _ticket(repo, 2, state="MergeQueued")
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.enqueue(WorkItem(ticket_id=tid, state_name="Planning", project="p"))
    qm.enqueue(WorkItem(ticket_id=other, state_name="MergeQueued", project="p"))

    qm.evict_merge_queued(tid)

    assert qm.queue_depth() == 2  # neither entry matched (tid isn't MergeQueued)
    first = qm.dequeue()
    assert first is not None and first.ticket_id == tid


def test_evict_merge_queued_is_idempotent_on_a_missing_entry(repo):
    """Calling evict for a ticket with no queued MergeQueued WorkItem is a no-op."""
    qm = QueueManager(repo=repo, max_in_flight=4)
    qm.evict_merge_queued(999)  # must not raise
    assert qm.queue_depth() == 0


def test_mark_done_releases_per_project_slot(repo):
    """After mark_done, a third ticket from the same capped project becomes dequeuable."""
    a = _ticket(repo, 1, project="proj")
    b = _ticket(repo, 2, project="proj")
    c = _ticket(repo, 3, project="proj")
    qm = QueueManager(repo=repo, max_in_flight=4, project_caps={"proj": 1})
    qm.enqueue(WorkItem(ticket_id=a, state_name="Planning", project="proj"))
    qm.enqueue(WorkItem(ticket_id=b, state_name="Planning", project="proj"))
    qm.enqueue(WorkItem(ticket_id=c, state_name="Planning", project="proj"))
    first = qm.dequeue()
    assert first is not None
    assert qm.dequeue() is None  # capped
    qm.mark_done(first)
    second = qm.dequeue()
    assert second is not None  # slot released
