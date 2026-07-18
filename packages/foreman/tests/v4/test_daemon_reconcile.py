"""Daemon wiring — startup reconciliation runs before the first tick.

A daemon restart (incl. Watchtower redeploy) must close any in-flight
``state_instances`` row left orphaned by the previous process BEFORE the
tick loop starts a worker thread. This proves ``run_forever`` calls
``reconcile_on_startup`` once at the top, ahead of the ``while`` loop.

foreman#550 Task 5: the same startup pass must also reconcile any
crash-orphaned ``merge_queue`` entry (``status="merging"``), AFTER the
state-instance reconcile — see the tests at the bottom of this file.
"""

from __future__ import annotations

import datetime as dt

import foreman.v4.daemon as daemon_module
from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.git_provider import FakeGitProvider, PRState
from foreman.v4.poller import Poller
from foreman.v4.reconcile import reconcile_on_startup
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.role_dispatcher import FakeRoleDispatcher


def test_run_forever_reconciles_before_ticking():
    """An in-flight orphan present at startup must be closed by
    ``run_forever`` (via the startup reconcile pass). We set the stop flag
    BEFORE calling ``run_forever`` so the startup reconcile runs and then the
    ``while not self._stop.is_set()`` loop body never ticks — isolating the
    reconcile pass. Deterministic, no real sleeps/network.
    """
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    def clock() -> dt.datetime:
        return now

    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=now)
    repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Implementing",
        sequence=1,
        now=now,
    )  # left in-flight, as a crash would leave it
    assert repo.list_in_flight_state_instances()  # precondition

    poller = Poller(
        repo=repo,
        qm=None,
        git=FakeGitProvider(),
        project="p",
        trigger_label="foreman:plan",
        clock=clock,
    )
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=clock,
    )

    # Set the stop flag first → run_forever runs the startup reconcile and
    # exits after at most one tick.
    daemon.stop()
    daemon.run_forever()

    assert repo.list_in_flight_state_instances() == []  # orphan closed at startup


def test_repeated_restarts_do_not_escalate_healthy_ticket():
    """Three crash/restart cycles on the same state must NOT trip the
    ``max_state_attempts=3`` cap, because each orphan is closed as
    ``crash_recovery`` and exempted from the runaway-cap counter.
    """
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    repo = InMemoryTicketRepository()
    t = repo.create_ticket(project="p", issue_number=1, now=now)
    for seq in (1, 2, 3):
        repo.open_state_instance(
            ticket_id=t.id,
            state_name="Implementing",
            sequence=seq,
            now=now,
        )
        reconcile_on_startup(repo, clock=lambda: now)  # simulate restart N

    assert (
        repo.count_consecutive_same_state(
            ticket_id=t.id,
            state="Implementing",
        )
        == 0
    )


# ---------------------------------------------------------------------------
# foreman#550 Task 5: merge_queue crash recovery, wired into the same
# startup path.
# ---------------------------------------------------------------------------


def test_run_forever_reconciles_merge_queue_on_startup():
    """A ``merge_queue`` entry left ``status="merging"`` by a crashed daemon
    whose PR actually merged must be recovered by ``run_forever``'s startup
    pass -- the ticket lands on its post-merge state and the entry is
    dequeued, exactly as ``MergeCoordinator.reconcile_on_startup`` does in
    isolation (see ``test_merge_coordinator.py``), but exercised through the
    full daemon wiring this time.
    """
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    def clock() -> dt.datetime:
        return now

    repo = InMemoryTicketRepository()
    git = FakeGitProvider()
    ticket = repo.create_ticket(project="p", issue_number=7, now=now)
    repo.set_ticket_state(ticket.id, "MergeQueued", now=now)
    entry = repo.enqueue_merge(project="p", ticket_id=ticket.id, pr_number=10, kind="impl", now=now)
    repo.mark_merge_active(entry.id)
    git.set_pr_state(
        project="p",
        pr_number=10,
        state=PRState(
            merged=True,
            mergeable=True,
            ci_passing=True,
            base_ref="main",
            mergeable_state="clean",
        ),
    )

    poller = Poller(
        repo=repo,
        qm=None,
        git=git,
        project="p",
        trigger_label="foreman:plan",
        clock=clock,
    )
    daemon = Daemon(
        repo=repo,
        git=git,
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=clock,
    )

    daemon.stop()
    daemon.run_forever()

    assert repo.get_ticket(ticket.id).current_state == "Done"
    assert repo.head_merge_entry("p") is None
    assert ("p", 7) in git.closed_issues


def test_reconcile_startup_runs_merge_coordinator_after_state_instance_reconcile(monkeypatch):
    """``_reconcile_startup`` must close orphaned ``state_instances`` rows
    BEFORE reconciling the ``merge_queue`` -- foreman#550 Task 5's ordering
    requirement, proven directly via call-order spies rather than inferred
    from side effects.
    """
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    def clock() -> dt.datetime:
        return now

    repo = InMemoryTicketRepository()
    poller = Poller(
        repo=repo,
        qm=None,
        git=FakeGitProvider(),
        project="p",
        trigger_label="foreman:plan",
        clock=clock,
    )
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=clock,
    )

    order: list[str] = []
    real_state_reconcile = daemon_module.reconcile_on_startup

    def spy_state_reconcile(repo_arg, *, clock):
        order.append("state_instances")
        return real_state_reconcile(repo_arg, clock=clock)

    monkeypatch.setattr(daemon_module, "reconcile_on_startup", spy_state_reconcile)

    real_merge_reconcile = daemon._merge_coordinator.reconcile_on_startup

    def spy_merge_reconcile() -> int:
        order.append("merge_queue")
        return real_merge_reconcile()

    monkeypatch.setattr(daemon._merge_coordinator, "reconcile_on_startup", spy_merge_reconcile)

    daemon.stop()
    daemon.run_forever()

    assert order == ["state_instances", "merge_queue"]
