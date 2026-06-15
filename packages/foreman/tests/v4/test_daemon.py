"""Daemon class — owns the Poller(s) + QM + WorkerPool tick loop."""
from __future__ import annotations

import datetime as dt
import threading
import time

from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.poller import Poller
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.sqlite_repository import SqliteTicketRepository


def _canned(kind: str, *, pr_number: int | None = None) -> str:
    art = f',"artifacts":{{"pr_number":{pr_number}}}' if pr_number else ""
    return f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high","summary":"x"{art}}}'


def test_daemon_one_tick_processes_one_ticket():
    repo = SqliteTicketRepository.in_memory()
    git = FakeGitProvider()
    git.set_open_issues_with_label(
        project="p", label="foreman:plan", issue_numbers={1},
    )
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "p", 1): _canned("clean"),
    })

    def clock() -> dt.datetime:
        return dt.datetime(2026, 6, 13, 12, 0, 0)

    poller = Poller(
        repo=repo, qm=None, git=git,
        project="p", trigger_label="foreman:plan",
        clock=clock,
    )
    daemon = Daemon(
        repo=repo, git=git, dispatcher=dispatcher,
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=clock,
    )
    daemon.tick_once()
    daemon.tick_once()
    ticket = repo.get_ticket_by_issue(project="p", issue_number=1)
    # After Queued advances to Planning (clean) → SpecReview
    assert ticket.current_state in ("Planning", "SpecReview")


def test_daemon_run_until_stopped_responds_to_stop_event():
    repo = SqliteTicketRepository.in_memory()
    poller = Poller(
        repo=repo, qm=None, git=FakeGitProvider(),
        project="p", trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    daemon = Daemon(
        repo=repo, git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0.01, max_in_flight=4),
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    thread = threading.Thread(target=daemon.run_forever)
    thread.start()
    time.sleep(0.05)
    daemon.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()
