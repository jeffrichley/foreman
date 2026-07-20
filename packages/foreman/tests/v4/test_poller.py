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


def test_blocked_ticket_dep_reconciled_and_gated():
    """Task 4: reconcile blocked_by into depends_on each poll.

    Tick 1: #291 is blocked by #290 (open) → depends_on=[290], not enqueued.
    Tick 2: #290 completed → depends_on=[], #291 IS enqueued.
    """
    repo = InMemoryTicketRepository()
    git = FakeGitProvider()

    # Seed the open ticket we track (issue #291).
    git.set_open_issues_with_label(project="p", label="foreman:plan", issue_numbers={291})
    # Issue #290 is a blocker that is currently open (no state_reason).
    git.set_blocked_by(project="p", issue_number=291, blocked_by=[290])
    # #290 has no state_reason → still open → unmet dep.

    qm = QueueManager(repo=repo, max_in_flight=4)
    poller = Poller(
        repo=repo,
        qm=qm,
        git=git,
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: _T0,
    )

    # Tick 1: adopt #291 + reconcile deps.
    poller.tick()

    ticket_291 = repo.get_ticket_by_issue(project="p", issue_number=291)
    # Dep must have been written.
    assert repo.get_ticket_dependencies(ticket_291.id) == [290]
    # Queue gate blocks dispatch while dep is unmet.
    assert qm.dequeue() is None

    # Now mark #290 closed-as-completed.
    git.set_issue_state_reason(project="p", issue_number=290, reason="completed")

    # Tick 2: reconciler clears depends_on; gate lifts.
    poller.tick()

    assert repo.get_ticket_dependencies(ticket_291.id) == []
    item = qm.dequeue()
    assert item is not None
    assert item.ticket_id == ticket_291.id


def test_mutual_cycle_holds_both_tickets_and_gates_dispatch():
    """Task 6: two tickets mutually blocked → both held with dependency cycle reason, neither dequeued.

    Setup: #1 is blocked_by #2, #2 is blocked_by #1. Both are tracked (in the plan label set).
    After a tick, both should be held with held_reason starting with "dependency cycle",
    and neither should be dequeued.
    """
    repo = InMemoryTicketRepository()
    git = FakeGitProvider()

    # Both tickets labeled and tracked.
    git.set_open_issues_with_label(project="p", label="foreman:plan", issue_numbers={1, 2})
    # Mutual block: #1 blocked_by #2, #2 blocked_by #1.
    git.set_blocked_by(project="p", issue_number=1, blocked_by=[2])
    git.set_blocked_by(project="p", issue_number=2, blocked_by=[1])

    qm = QueueManager(repo=repo, max_in_flight=4)
    poller = Poller(
        repo=repo,
        qm=qm,
        git=git,
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: _T0,
    )

    poller.tick()

    t1 = repo.get_ticket_by_issue(project="p", issue_number=1)
    t2 = repo.get_ticket_by_issue(project="p", issue_number=2)

    # Both tickets must be held.
    assert t1.is_held, "ticket #1 should be held due to dependency cycle"
    assert t2.is_held, "ticket #2 should be held due to dependency cycle"

    # held_reason must reference dependency cycle.
    assert t1.held_reason is not None and t1.held_reason.startswith("dependency cycle")
    assert t2.held_reason is not None and t2.held_reason.startswith("dependency cycle")

    # Neither ticket dequeued.
    assert qm.dequeue() is None, "no ticket should be dispatched when both are cycle-held"


def test_cycle_hold_is_idempotent_across_ticks():
    """Task 6: a second tick does NOT re-hold an already cycle-held ticket.

    After tick 1 both tickets are held. Tick 2 must not change held_reason
    (no redundant hold_ticket call that would overwrite with a new timestamp).
    """
    repo = InMemoryTicketRepository()
    git = FakeGitProvider()

    git.set_open_issues_with_label(project="p", label="foreman:plan", issue_numbers={1, 2})
    git.set_blocked_by(project="p", issue_number=1, blocked_by=[2])
    git.set_blocked_by(project="p", issue_number=2, blocked_by=[1])

    qm = QueueManager(repo=repo, max_in_flight=4)
    poller = Poller(
        repo=repo,
        qm=qm,
        git=git,
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: _T0,
    )

    poller.tick()

    t1_after_tick1 = repo.get_ticket_by_issue(project="p", issue_number=1)
    held_at_tick1 = t1_after_tick1.held_at

    poller.tick()

    t1_after_tick2 = repo.get_ticket_by_issue(project="p", issue_number=1)
    # held_at should not have changed (no redundant hold).
    assert t1_after_tick2.held_at == held_at_tick1, (
        "hold_ticket must not be called again when reason already starts with 'dependency cycle'"
    )


def test_cycle_hold_self_heals_when_cycle_cleared():
    """Bug 1 fix: when the cycle is broken, the orchestrator auto-resumes held tickets.

    Tick 1: #1 ↔ #2 mutual cycle → both held with "dependency cycle" reason.
    Clear one side (#2's blocked_by removed → cycle gone).
    Tick 2: both tickets must be resumed (is_held is False) and dispatchable.
    """
    repo = InMemoryTicketRepository()
    git = FakeGitProvider()

    git.set_open_issues_with_label(project="p", label="foreman:plan", issue_numbers={1, 2})
    git.set_blocked_by(project="p", issue_number=1, blocked_by=[2])
    git.set_blocked_by(project="p", issue_number=2, blocked_by=[1])

    qm = QueueManager(repo=repo, max_in_flight=4)
    poller = Poller(
        repo=repo,
        qm=qm,
        git=git,
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: _T0,
    )

    # Tick 1: form the cycle → both held, neither dequeued.
    poller.tick()

    t1 = repo.get_ticket_by_issue(project="p", issue_number=1)
    t2 = repo.get_ticket_by_issue(project="p", issue_number=2)
    assert t1.is_held, "ticket #1 should be cycle-held after tick 1"
    assert t2.is_held, "ticket #2 should be cycle-held after tick 1"
    assert qm.dequeue() is None

    # Break the cycle: clear BOTH sides' blocked_by so neither dep remains.
    git.set_blocked_by(project="p", issue_number=1, blocked_by=[])
    git.set_blocked_by(project="p", issue_number=2, blocked_by=[])

    # Tick 2: cycle is gone → orchestrator auto-resumes both.
    poller.tick()

    t1 = repo.get_ticket_by_issue(project="p", issue_number=1)
    t2 = repo.get_ticket_by_issue(project="p", issue_number=2)
    assert not t1.is_held, "ticket #1 should be auto-resumed after cycle cleared"
    assert not t2.is_held, "ticket #2 should be auto-resumed after cycle cleared"

    # Both should now be dispatchable (no hold, no unmet deps).
    dispatched_ids = set()
    while (item := qm.dequeue()) is not None:
        dispatched_ids.add(item.ticket_id)
    assert t1.id in dispatched_ids, "ticket #1 should be enqueued after self-heal"
    assert t2.id in dispatched_ids, "ticket #2 should be enqueued after self-heal"


def test_cycle_hold_preserves_operator_hold():
    """Bug 2 fix: a cycle detection must not overwrite an existing operator hold.

    Setup:
    - Operator holds ticket #1 with reason "investigating".
    - Then #1 ↔ #2 form a mutual cycle.
    - Tick → #1's hold must remain "investigating" (NOT overwritten by cycle reason).
    - After cycle clears, #1 remains held (operator hold is NOT auto-resumed).
    """
    repo = InMemoryTicketRepository()
    git = FakeGitProvider()

    git.set_open_issues_with_label(project="p", label="foreman:plan", issue_numbers={1, 2})
    git.set_blocked_by(project="p", issue_number=1, blocked_by=[2])
    git.set_blocked_by(project="p", issue_number=2, blocked_by=[1])

    qm = QueueManager(repo=repo, max_in_flight=4)
    poller = Poller(
        repo=repo,
        qm=qm,
        git=git,
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: _T0,
    )

    # Adopt the tickets first (tick creates them).
    poller.tick()

    # Simulate operator manually holding #1 AFTER the cycle hold ran.
    # Override with the operator's hold.
    t1 = repo.get_ticket_by_issue(project="p", issue_number=1)
    repo.resume_ticket(t1.id, now=_T0)  # clear cycle hold first
    repo.hold_ticket(t1.id, held_by="jeff", reason="investigating", now=_T0)

    # Tick again: cycle still present → must NOT overwrite #1's operator hold.
    poller.tick()

    t1_after = repo.get_ticket_by_issue(project="p", issue_number=1)
    assert t1_after.held_by == "jeff", (
        "operator hold must be preserved: held_by should still be 'jeff'"
    )
    assert t1_after.held_reason == "investigating", (
        "operator hold must be preserved: held_reason should still be 'investigating'"
    )

    # Now clear the cycle — #1's operator hold must NOT be auto-resumed.
    git.set_blocked_by(project="p", issue_number=2, blocked_by=[])
    poller.tick()

    t1_after_clear = repo.get_ticket_by_issue(project="p", issue_number=1)
    assert t1_after_clear.is_held, (
        "operator hold on #1 must persist even after cycle clears (not auto-resumed)"
    )
    assert t1_after_clear.held_reason == "investigating", (
        "operator hold reason must still be 'investigating' after cycle clears"
    )


def test_poller_does_not_reconcile_foreign_project_tickets():
    """A per-project Poller must reconcile ONLY its own project's tickets.

    Production wiring gives each Poller its own repo-locked GitProvider that
    ignores the ``project=`` kwarg (unlike the project-aware fake here). With a
    global, un-scoped open-ticket list, every Poller reconciled every OTHER
    project's tickets through the wrong repo — reading empty blocker lists and
    clobbering their ``depends_on`` to ``[]``, silently ungating ``blocked_by``
    in any multi-project deployment.

    This pins the fix: project "a"'s Poller reconciles its own ticket but
    leaves project "b"'s ticket untouched. See foreman#568.
    """
    repo = InMemoryTicketRepository()

    # Project "b" ticket #10, already correctly reconciled to depends_on=[9]
    # by project b's own Poller.
    ticket_b = repo.create_ticket(project="b", issue_number=10, now=_T0)
    repo.set_ticket_dependencies(ticket_b.id, deps=[9])

    # Project "a"'s repo-locked provider: it knows project a (issue #1 blocked
    # by the still-open #2) and NOTHING about project b — so a cross-project
    # read of b#10 returns [] (the production clobber value).
    git_a = FakeGitProvider()
    git_a.set_open_issues_with_label(project="a", label="foreman:plan", issue_numbers={1})
    git_a.set_blocked_by(project="a", issue_number=1, blocked_by=[2])

    qm = QueueManager(repo=repo, max_in_flight=4)
    poller_a = Poller(
        repo=repo,
        qm=qm,
        git=git_a,
        project="a",
        trigger_label="foreman:plan",
        clock=lambda: _T0,
    )

    poller_a.tick()

    # Project a's own ticket IS reconciled (blocker #2 open → unmet dep).
    ticket_a = repo.get_ticket_by_issue(project="a", issue_number=1)
    assert repo.get_ticket_dependencies(ticket_a.id) == [2]

    # Project b's ticket is UNTOUCHED — project a's Poller must not clobber it.
    assert repo.get_ticket_dependencies(ticket_b.id) == [9]
