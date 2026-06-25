"""Daemon wiring — startup reconciliation runs before the first tick.

A daemon restart (incl. Watchtower redeploy) must close any in-flight
``state_instances`` row left orphaned by the previous process BEFORE the
tick loop starts a worker thread. This proves ``run_forever`` calls
``reconcile_on_startup`` once at the top, ahead of the ``while`` loop.
"""
from __future__ import annotations

import datetime as dt

from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.poller import Poller
from foreman.v4.reconcile import reconcile_on_startup
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.sqlite_repository import SqliteTicketRepository


def test_run_forever_reconciles_before_ticking():
    """An in-flight orphan present at startup must be closed by
    ``run_forever`` (via the startup reconcile pass). We set the stop flag
    BEFORE calling ``run_forever`` so the loop runs the startup reconcile +
    at most one tick, then exits — deterministic, no real sleeps/network.
    """
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    def clock() -> dt.datetime:
        return now

    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=1, now=now)
    repo.open_state_instance(
        ticket_id=ticket.id, state_name="Implementing", sequence=1, now=now,
    )  # left in-flight, as a crash would leave it
    assert repo.list_in_flight_state_instances()  # precondition

    poller = Poller(
        repo=repo, qm=None, git=FakeGitProvider(),
        project="p", trigger_label="foreman:plan",
        clock=clock,
    )
    daemon = Daemon(
        repo=repo, git=FakeGitProvider(),
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
    repo = SqliteTicketRepository.in_memory()
    t = repo.create_ticket(project="p", issue_number=1, now=now)
    for seq in (1, 2, 3):
        repo.open_state_instance(
            ticket_id=t.id, state_name="Implementing", sequence=seq, now=now,
        )
        reconcile_on_startup(repo, clock=lambda: now)  # simulate restart N

    assert (
        repo.count_consecutive_same_state(
            ticket_id=t.id, state="Implementing",
        )
        == 0
    )
