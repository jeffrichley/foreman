"""Poller — single sweep that turns SQLite + GitHub state into WorkItems."""

from __future__ import annotations

import datetime as dt

from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.poller import Poller
from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.work import WorkItem

_T0 = dt.datetime(2026, 6, 13, 12, 0, 0)


def _make_poller(repo, git):
    qm = QueueManager(repo=repo, max_in_flight=4)
    poller = Poller(
        repo=repo,
        qm=qm,
        git=git,
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: _T0,
    )
    return poller, qm


def test_new_labeled_issue_creates_ticket_and_enqueues():
    repo = InMemoryTicketRepository()
    git = FakeGitProvider()
    git.set_open_issues_with_label(
        project="p",
        label="foreman:plan",
        issue_numbers={42},
    )
    poller, qm = _make_poller(repo, git)
    poller.tick()
    # Ticket created:
    ticket = repo.get_ticket_by_issue(project="p", issue_number=42)
    assert ticket.current_state == "Queued"
    # Work enqueued:
    assert qm.dequeue() == WorkItem(ticket_id=ticket.id, state_name="Queued", project="p")


def test_existing_ticket_not_duplicated():
    repo = InMemoryTicketRepository()
    repo.create_ticket(project="p", issue_number=42, now=_T0)
    git = FakeGitProvider()
    git.set_open_issues_with_label(
        project="p",
        label="foreman:plan",
        issue_numbers={42},
    )
    poller, qm = _make_poller(repo, git)
    poller.tick()
    poller.tick()
    # Second tick should not create a second ticket — TicketAlreadyExistsError
    # would have been raised on insert otherwise.
    ticket = repo.get_ticket_by_issue(project="p", issue_number=42)
    assert ticket.id == 1


def test_in_flight_non_blocked_state_re_enqueued_for_advance():
    repo = InMemoryTicketRepository()
    t = repo.create_ticket(project="p", issue_number=1, now=_T0)
    repo.set_ticket_state(t.id, "Planning", now=_T0)
    git = FakeGitProvider()
    poller, qm = _make_poller(repo, git)
    poller.tick()
    assert qm.dequeue() == WorkItem(ticket_id=t.id, state_name="Planning", project="p")


def test_terminal_states_not_enqueued():
    repo = InMemoryTicketRepository()
    for issue, state in (
        (101, "Done"),
        (102, "Failed"),
        (103, "NeedsHelp"),
    ):
        t = repo.create_ticket(project="p", issue_number=issue, now=_T0)
        repo.set_ticket_state(t.id, state, now=_T0)
    git = FakeGitProvider()
    poller, qm = _make_poller(repo, git)
    poller.tick()
    assert qm.dequeue() is None


def test_dedup_across_repeated_ticks():
    """Three identical ticks should leave the QM with at most one WorkItem per ticket."""
    repo = InMemoryTicketRepository()
    t = repo.create_ticket(project="p", issue_number=1, now=_T0)
    repo.set_ticket_state(t.id, "Planning", now=_T0)
    git = FakeGitProvider()
    poller, qm = _make_poller(repo, git)
    poller.tick()
    poller.tick()
    poller.tick()
    assert qm.dequeue() == WorkItem(ticket_id=t.id, state_name="Planning", project="p")
    assert qm.dequeue() is None


def test_poller_skips_suspended_ticket():
    """foreman#361: a ticket whose ``next_action_at`` is in the future
    MUST NOT be enqueued. Once the clock advances past
    ``next_action_at``, the next tick enqueues normally.
    """
    repo = InMemoryTicketRepository()
    t = repo.create_ticket(project="p", issue_number=1, now=_T0)
    repo.set_ticket_state(t.id, "Planning", now=_T0)
    suspend_until = _T0 + dt.timedelta(minutes=5)
    repo.set_next_action_at(t.id, when=suspend_until)
    git = FakeGitProvider()

    # First tick: clock is at _T0 (before suspend_until) → no enqueue.
    qm = QueueManager(repo=repo, max_in_flight=4)
    early_poller = Poller(
        repo=repo,
        qm=qm,
        git=git,
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: _T0,
    )
    early_poller.tick()
    assert qm.dequeue() is None

    # Second tick: clock advanced past suspend_until → enqueued.
    later_qm = QueueManager(repo=repo, max_in_flight=4)
    later_poller = Poller(
        repo=repo,
        qm=later_qm,
        git=git,
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: suspend_until + dt.timedelta(seconds=1),
    )
    later_poller.tick()
    assert later_qm.dequeue() == WorkItem(ticket_id=t.id, state_name="Planning", project="p")
