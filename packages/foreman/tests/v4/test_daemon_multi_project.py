"""Daemon with multiple Pollers — one per project, shared QM + WorkerPool.

Phase 6.5 built the Daemon to hold a `list[Poller]`. This test proves
the multi-project shape works end-to-end: two Pollers (one per project)
sharing one QueueManager + one WorkerPool advance independent tickets
through the tick loop.
"""

from __future__ import annotations

import datetime as dt

from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.poller import Poller
from foreman.v4.role_dispatcher import FakeRoleDispatcher
from foreman.v4.sqlite_repository import SqliteTicketRepository


def _canned(kind: str) -> str:
    return f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high","summary":"x"}}'


def test_tick_polls_every_project_and_advances_each() -> None:
    """Two Pollers (voice + foreman) each surface one issue; the Daemon
    advances both past Queued in two ticks.
    """
    repo = SqliteTicketRepository.in_memory()
    git = FakeGitProvider()
    # One labeled issue per project.
    git.set_open_issues_with_label(
        project="voice", label="foreman:plan", issue_numbers={1},
    )
    git.set_open_issues_with_label(
        project="foreman", label="foreman:plan", issue_numbers={2},
    )
    dispatcher = FakeRoleDispatcher(responses={
        ("planner", "voice", 1): _canned("clean"),
        ("planner", "foreman", 2): _canned("clean"),
    })

    def clock() -> dt.datetime:
        return dt.datetime(2026, 6, 13, 12, 0, 0)

    daemon = Daemon(
        repo=repo,
        git=git,
        dispatcher=dispatcher,
        pollers=[
            Poller(
                repo=repo, qm=None, git=git, project="voice",
                trigger_label="foreman:plan", clock=clock,
            ),
            Poller(
                repo=repo, qm=None, git=git, project="foreman",
                trigger_label="foreman:plan", clock=clock,
            ),
        ],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=clock,
    )
    # Pollers built without QM get the QM wired by the Daemon constructor.
    # First tick adopts both new issues + enqueues Queued; second tick
    # runs Planning for each (clean → SpecReview).
    daemon.tick_once()
    daemon.tick_once()

    voice = repo.get_ticket_by_issue(project="voice", issue_number=1)
    foreman_t = repo.get_ticket_by_issue(project="foreman", issue_number=2)
    assert voice is not None
    assert foreman_t is not None
    assert voice.current_state != "Queued", (
        f"voice ticket did not advance — still in {voice.current_state}"
    )
    assert foreman_t.current_state != "Queued", (
        f"foreman ticket did not advance — still in {foreman_t.current_state}"
    )
