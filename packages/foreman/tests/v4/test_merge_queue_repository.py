"""Tests for the persistent per-project merge_queue (foreman#550 Task 2).

Targets InMemoryTicketRepository only — PostgresTicketRepository can't be
unit-tested without a live DB; its conformance is checked by mypy strict
and by manual review against the psycopg patterns used elsewhere in
postgres_repository.py.
"""

from __future__ import annotations

import datetime as dt

from foreman.v4.repository import InMemoryTicketRepository


def _t(n: int) -> dt.datetime:
    return dt.datetime(2026, 7, 17, 0, 0, n)


def test_enqueue_and_fifo_order() -> None:
    r = InMemoryTicketRepository()
    a = r.enqueue_merge(project="p", ticket_id=1, pr_number=10, kind="impl", now=_t(1))
    r.enqueue_merge(project="p", ticket_id=2, pr_number=11, kind="spec", now=_t(2))
    assert [e.pr_number for e in r.merge_queue_for_project("p")] == [10, 11]
    head = r.head_merge_entry("p")
    assert head is not None
    assert head.id == a.id


def test_mark_active_attempts_and_dequeue() -> None:
    r = InMemoryTicketRepository()
    e = r.enqueue_merge(project="p", ticket_id=1, pr_number=10, kind="impl", now=_t(1))
    r.mark_merge_active(e.id)
    head = r.head_merge_entry("p")
    assert head is not None
    assert head.status == "merging"
    assert r.increment_merge_attempts(e.id) == 1
    assert [x.id for x in r.list_active_merges()] == [e.id]
    r.dequeue_merge(e.id)
    assert r.head_merge_entry("p") is None


def test_per_project_isolation() -> None:
    r = InMemoryTicketRepository()
    r.enqueue_merge(project="a", ticket_id=1, pr_number=10, kind="impl", now=_t(1))
    assert r.head_merge_entry("b") is None
