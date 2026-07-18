"""Daemon class — owns the Poller(s) + QM + WorkerPool tick loop."""

from __future__ import annotations

import datetime as dt
import threading
import time
from unittest.mock import MagicMock

from foreman.v4.daemon import Daemon, DaemonConfig
from foreman.v4.git_provider import FakeGitProvider
from foreman.v4.poller import Poller
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.role_dispatcher import FakeRoleDispatcher


def _canned(kind: str, *, pr_number: int | None = None) -> str:
    art = f',"artifacts":{{"pr_number":{pr_number}}}' if pr_number else ""
    return f'FOREMAN_OUTCOME:{{"kind":"{kind}","confidence":"high","summary":"x"{art}}}'


def test_daemon_one_tick_processes_one_ticket():
    repo = InMemoryTicketRepository()
    git = FakeGitProvider()
    git.set_open_issues_with_label(
        project="p",
        label="foreman:plan",
        issue_numbers={1},
    )
    dispatcher = FakeRoleDispatcher(
        responses={
            ("planner", "p", 1): _canned("clean"),
        }
    )

    def clock() -> dt.datetime:
        return dt.datetime(2026, 6, 13, 12, 0, 0)

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
        dispatcher=dispatcher,
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
    repo = InMemoryTicketRepository()
    poller = Poller(
        repo=repo,
        qm=None,
        git=FakeGitProvider(),
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
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


def test_daemon_shutdown_waits_for_in_flight_work():
    """``shutdown(wait=True)`` must not return until the in-flight transition
    completes. Mirrors the BlockingDispatcher pattern from test_worker_pool —
    submit a transition that parks on an Event, kick shutdown from a side
    thread, and assert it doesn't return early.
    """
    repo = InMemoryTicketRepository()
    git = FakeGitProvider()
    git.set_open_issues_with_label(
        project="p",
        label="foreman:plan",
        issue_numbers={1},
    )

    release = threading.Event()
    base = FakeRoleDispatcher(
        responses={
            ("planner", "p", 1): _canned("clean"),
        }
    )

    class BlockingDispatcher:
        def dispatch(self, **kwargs):
            release.wait(timeout=5.0)
            return base.dispatch(**kwargs)

    def clock() -> dt.datetime:
        return dt.datetime(2026, 6, 13, 12, 0, 0)

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
        dispatcher=BlockingDispatcher(),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=clock,
    )

    # First tick_once polls + pool-ticks + drains: it adopts issue 1,
    # transitions Queued → Planning (Queued does no role-call so the
    # BlockingDispatcher isn't hit), and returns with the queue empty.
    daemon.tick_once()

    # Re-poll to enqueue the Planning WorkItem for the now-open ticket
    # (the Poller enqueues current_state for every non-terminal open
    # ticket on each tick), then pool-tick to submit it. Planning IS a
    # role-dispatch state, so this future parks on the release Event.
    for p in daemon._pollers:
        p.tick()
    submitted = daemon._pool.tick()
    assert submitted == 1, f"expected Planning WorkItem to be submitted; submitted={submitted}"
    # Confirm Planning is actually in-flight, parked on the release Event.
    assert daemon.qm.in_flight_count() == 1, (
        "expected Planning transition to be in-flight before shutdown"
    )

    shutdown_returned = threading.Event()

    def _do_shutdown() -> None:
        daemon.shutdown(wait=True)
        shutdown_returned.set()

    shutdown_thread = threading.Thread(target=_do_shutdown)
    shutdown_thread.start()
    try:
        # Give shutdown a real window to (incorrectly) return early.
        # 100ms is plenty — release Event has a 5s timeout inside dispatch.
        assert not shutdown_returned.wait(timeout=0.1), (
            "shutdown(wait=True) returned before in-flight work completed"
        )
        # Release the parked transition; shutdown should now return.
        release.set()
        assert shutdown_returned.wait(timeout=5.0), (
            "shutdown(wait=True) never returned after work was released"
        )
    finally:
        release.set()
        shutdown_thread.join(timeout=5.0)


def test_daemon_shutdown_is_idempotent():
    """Calling shutdown twice must not raise — second call is a no-op
    (delegates to a ThreadPoolExecutor whose shutdown is documented
    safe to call repeatedly).
    """
    repo = InMemoryTicketRepository()
    poller = Poller(
        repo=repo,
        qm=None,
        git=FakeGitProvider(),
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    daemon.shutdown(wait=True)
    daemon.shutdown(wait=True)  # must not raise
    assert daemon.qm.in_flight_count() == 0


# --- Issue #407: clone refresher wiring on tick_once -------------------


def test_tick_once_calls_clone_refresher():
    """foreman#407: tick_once must invoke the injected clone refresher
    exactly once per tick — proving the per-poll clone refresh runs in
    the daemon loop, NOT only inside worktree.create()."""
    repo = InMemoryTicketRepository()
    stub_refresher = MagicMock()
    stub_refresher.tick.return_value = None
    poller = Poller(
        repo=repo,
        qm=None,
        git=FakeGitProvider(),
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
        clone_refresher=stub_refresher,
    )
    daemon.tick_once()
    assert stub_refresher.tick.call_count == 1


def test_tick_once_runs_with_disabled_clone_refresher_default():
    """No explicit ``clone_refresher=`` kwarg → no-op sentinel default;
    ``tick_once`` must not raise."""
    repo = InMemoryTicketRepository()
    poller = Poller(
        repo=repo,
        qm=None,
        git=FakeGitProvider(),
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )
    daemon.tick_once()  # must not raise


# --- Issue #434: backup scheduler wiring on tick_once -------------------


def test_tick_once_calls_backup_scheduler():
    """foreman#434: tick_once must invoke the injected backup scheduler
    exactly once per tick."""
    repo = InMemoryTicketRepository()
    stub_scheduler = MagicMock()
    stub_scheduler.tick.return_value = None
    poller = Poller(
        repo=repo,
        qm=None,
        git=FakeGitProvider(),
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 6, 27, 12, 0, 0),
    )
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=lambda: dt.datetime(2026, 6, 27, 12, 0, 0),
        backup_scheduler=stub_scheduler,
    )
    daemon.tick_once()
    assert stub_scheduler.tick.call_count == 1


def test_tick_once_uses_disabled_scheduler_by_default():
    """No explicit ``backup_scheduler=`` kwarg → no-op sentinel default;
    ``tick_once`` must not raise."""
    repo = InMemoryTicketRepository()
    poller = Poller(
        repo=repo,
        qm=None,
        git=FakeGitProvider(),
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 6, 27, 12, 0, 0),
    )
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=lambda: dt.datetime(2026, 6, 27, 12, 0, 0),
    )
    daemon.tick_once()  # must not raise


# --- foreman I1 (self-heal arch-review 2026-07-17): tick fault isolation ---
#
# Before this, an unhandled error in a synchronous tick step (a poller's
# GitHub sweep, the pool submission) propagated out of ``tick_once`` and
# ``run_forever`` and killed the daemon. Under Docker ``restart:
# unless-stopped`` a deterministic error became a crash loop that took down
# EVERY project. These assert the boundaries that keep one failure local.


def test_tick_once_isolates_a_failing_poller():
    """One poller raising must not abort the tick: the other projects' pollers
    still run and ``tick_once`` does not propagate."""
    repo = InMemoryTicketRepository()
    bad = MagicMock()
    bad._project = "bad"
    bad._qm = None
    bad.tick.side_effect = RuntimeError("gh sweep boom")
    good = MagicMock()
    good._project = "good"
    good._qm = None
    good.tick.return_value = None
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[bad, good],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=lambda: dt.datetime(2026, 7, 17, 12, 0, 0),
    )
    daemon.tick_once()  # must not raise despite bad.tick() raising
    bad.tick.assert_called_once()
    good.tick.assert_called_once()  # isolation: good still ran


def test_tick_once_isolates_a_failing_pool_tick():
    """An error in the WorkerPool submission tick is logged, not propagated."""
    repo = InMemoryTicketRepository()
    poller = Poller(
        repo=repo,
        qm=None,
        git=FakeGitProvider(),
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 7, 17, 12, 0, 0),
    )
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=lambda: dt.datetime(2026, 7, 17, 12, 0, 0),
    )
    boom = MagicMock(side_effect=RuntimeError("pool boom"))
    daemon._pool.tick = boom  # type: ignore[method-assign]
    daemon.tick_once()  # must not raise
    boom.assert_called_once()


def test_run_forever_survives_a_tick_error_and_continues():
    """run_forever must never die from a tick exception: a raising tick is
    logged and the loop proceeds. A tick_once that raises once then stops the
    loop on its second call proves the first error was caught, not propagated
    (reaching call 2 is only possible if call 1 did not escape the loop)."""
    repo = InMemoryTicketRepository()
    poller = Poller(
        repo=repo,
        qm=None,
        git=FakeGitProvider(),
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 7, 17, 12, 0, 0),
    )
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=lambda: dt.datetime(2026, 7, 17, 12, 0, 0),
    )
    calls = {"n": 0}

    def flaky_tick() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tick boom")
        daemon.stop()  # survived the error → end the loop

    daemon.tick_once = flaky_tick  # type: ignore[method-assign]
    daemon.run_forever()  # must return, not raise
    assert calls["n"] == 2  # reached a 2nd tick ⇒ first error was isolated


# --- foreman#550: MergeCoordinator wiring ---


def test_daemon_constructs_a_merge_coordinator():
    repo = InMemoryTicketRepository()
    poller = Poller(
        repo=repo,
        qm=None,
        git=FakeGitProvider(),
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 7, 17, 12, 0, 0),
    )
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=lambda: dt.datetime(2026, 7, 17, 12, 0, 0),
    )
    from foreman.v4.merge_coordinator import MergeCoordinator

    assert isinstance(daemon._merge_coordinator, MergeCoordinator)


def test_tick_once_calls_the_merge_coordinator_once():
    repo = InMemoryTicketRepository()
    poller = Poller(
        repo=repo,
        qm=None,
        git=FakeGitProvider(),
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 7, 17, 12, 0, 0),
    )
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=lambda: dt.datetime(2026, 7, 17, 12, 0, 0),
    )
    calls = MagicMock()
    daemon._merge_coordinator.tick = calls  # type: ignore[method-assign]
    daemon.tick_once()
    calls.assert_called_once()


def test_tick_once_isolates_a_failing_merge_coordinator():
    """A coordinator error (GitHub API blip, repo hiccup) must not propagate
    out of tick_once — mirrors the poller/pool isolation boundaries (#540/I1)."""
    repo = InMemoryTicketRepository()
    poller = Poller(
        repo=repo,
        qm=None,
        git=FakeGitProvider(),
        project="p",
        trigger_label="foreman:plan",
        clock=lambda: dt.datetime(2026, 7, 17, 12, 0, 0),
    )
    daemon = Daemon(
        repo=repo,
        git=FakeGitProvider(),
        dispatcher=FakeRoleDispatcher(responses={}),
        pollers=[poller],
        config=DaemonConfig(tick_seconds=0, max_in_flight=4),
        clock=lambda: dt.datetime(2026, 7, 17, 12, 0, 0),
    )
    boom = MagicMock(side_effect=RuntimeError("coordinator boom"))
    daemon._merge_coordinator.tick = boom  # type: ignore[method-assign]
    daemon.tick_once()  # must not raise
    boom.assert_called_once()
