"""Tests for the daemon worker iteration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from foreman.config import AppsConfig, ProjectConfig
from foreman.dispatcher import Action, ActionKind, Ticket
from foreman.locks import TicketLockManager
from foreman.queue import DaemonQueue
from foreman.storage import Storage
from foreman.worker import RoleResult, run_one_iteration


@dataclass
class _FakeRoleDispatcher:
    calls: list[tuple[Ticket, Action]] = field(default_factory=list)
    result_factory: Any = None

    async def dispatch(self, *, ticket: Ticket, action: Action) -> RoleResult:
        self.calls.append((ticket, action))
        if self.result_factory is not None:
            return self.result_factory(ticket, action)
        return RoleResult(
            new_labels=frozenset({"foreman:spec-review"}),
            structured_output={"pr_number": 1},
            outcome="success",
        )


def _ticket() -> Ticket:
    return Ticket(
        project_name="voice",
        issue_number=42,
        labels=frozenset({"foreman:plan"}),
        last_transition_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def _project_configs() -> dict[str, ProjectConfig]:
    return {
        "voice": ProjectConfig(
            repo="jeffrichley/voice",
            local_clone_path="/tmp/voice",
            apps=AppsConfig(),
        )
    }


@pytest.mark.asyncio
async def test_run_one_iteration_dispatches_action_when_queue_has_work(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()
    dispatcher = _FakeRoleDispatcher()
    projects = _project_configs()

    queue.enqueue(_ticket())

    advanced = await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )

    assert advanced is True
    assert len(dispatcher.calls) == 1
    ticket, action = dispatcher.calls[0]
    assert ticket.issue_number == 42
    assert action.kind == ActionKind.RUN_PLANNER


@pytest.mark.asyncio
async def test_run_one_iteration_returns_false_when_queue_empty(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()
    dispatcher = _FakeRoleDispatcher()
    projects = _project_configs()

    advanced = await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )

    assert advanced is False
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_run_one_iteration_persists_node_run_and_transition(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()
    dispatcher = _FakeRoleDispatcher()
    projects = _project_configs()

    queue.enqueue(_ticket())

    await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )

    with storage.connect() as conn:
        node_runs = list(conn.execute("SELECT * FROM node_runs"))
        transitions = list(conn.execute("SELECT * FROM transitions"))
    assert len(node_runs) == 1
    assert node_runs[0]["role"] == "planner"
    assert node_runs[0]["outcome"] == "success"
    assert len(transitions) == 1


@pytest.mark.asyncio
async def test_run_one_iteration_re_enqueues_when_more_work_remains(tmp_path: Path) -> None:
    """After role completes with new actionable labels, ticket goes back to queue."""
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()
    dispatcher = _FakeRoleDispatcher()
    projects = _project_configs()

    queue.enqueue(_ticket())
    await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )

    assert len(queue) == 1
    snap = queue.snapshot()
    assert snap[0].labels == frozenset({"foreman:spec-review"})


@pytest.mark.asyncio
async def test_run_one_iteration_does_not_re_enqueue_parked_ticket(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()

    def _result(ticket: Ticket, action: Action) -> RoleResult:
        return RoleResult(
            new_labels=frozenset({"foreman:spec-ready"}),
            structured_output=None,
            outcome="success",
        )

    dispatcher = _FakeRoleDispatcher(result_factory=_result)
    projects = _project_configs()  # auto_merge_spec=False by default

    queue.enqueue(_ticket())
    await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )

    assert len(queue) == 0


@pytest.mark.asyncio
async def test_run_one_iteration_records_failure_and_marks_failed(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()

    def _raise(ticket: Ticket, action: Action) -> RoleResult:
        raise RuntimeError("simulated role crash")

    dispatcher = _FakeRoleDispatcher(result_factory=_raise)
    projects = _project_configs()

    queue.enqueue(_ticket())
    await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )

    with storage.connect() as conn:
        failures = list(conn.execute("SELECT * FROM failures"))
    assert len(failures) == 1
    assert "simulated role crash" in failures[0]["reason"]


@dataclass
class _HangingDispatcher:
    """Dispatch coroutine that never completes — simulates a wedged role."""

    calls: int = 0

    async def dispatch(self, *, ticket: Ticket, action: Action) -> RoleResult:
        self.calls += 1
        import asyncio as _asyncio

        await _asyncio.sleep(60)
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_run_one_iteration_times_out_hung_dispatch(tmp_path: Path) -> None:
    """A hanging role dispatch must be cancelled at ``timeout_seconds`` and
    recorded as a TimeoutError failure — otherwise the single worker stalls
    forever and the daemon stops making progress.
    """
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()
    dispatcher = _HangingDispatcher()
    projects = _project_configs()

    queue.enqueue(_ticket())
    advanced = await run_one_iteration(
        queue=queue,
        locks=locks,
        dispatcher=dispatcher,
        storage=storage,
        projects=projects,
        timeout_seconds=0.05,
    )

    assert advanced is True
    assert dispatcher.calls == 1
    with storage.connect() as conn:
        failures = list(conn.execute("SELECT * FROM failures"))
        node_runs = list(conn.execute("SELECT * FROM node_runs"))
    assert len(failures) == 1
    assert "TimeoutError" in failures[0]["reason"]
    assert len(node_runs) == 1
    assert node_runs[0]["outcome"] == "failure"


@pytest.mark.asyncio
async def test_run_one_iteration_without_timeout_does_not_wrap(tmp_path: Path) -> None:
    """When ``timeout_seconds=None`` (the default), dispatch runs unbounded —
    pin the contract so a future bug doesn't silently introduce a default
    timeout that breaks long-running roles like the Worker.
    """
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()
    dispatcher = _FakeRoleDispatcher()
    projects = _project_configs()

    queue.enqueue(_ticket())
    advanced = await run_one_iteration(
        queue=queue,
        locks=locks,
        dispatcher=dispatcher,
        storage=storage,
        projects=projects,
    )

    assert advanced is True
    assert len(dispatcher.calls) == 1
    with storage.connect() as conn:
        failures = list(conn.execute("SELECT * FROM failures"))
    assert failures == []


@pytest.mark.asyncio
async def test_run_one_iteration_does_not_re_dispatch_on_stale_labels(
    tmp_path: Path,
) -> None:
    """foreman#91: after a Reviewer-on-impl success transition
    (``foreman:impl-review`` → ``foreman:ready-for-merge``), the worker
    must NOT re-dispatch ``run_reviewer_impl`` on stale labels. The
    role's authoritative ``final_labels`` (via
    ``DaemonRunners.RoleResult.new_labels``) is what the next iteration
    sees — not a stale GitHub re-read.

    End-to-end shape: enqueue a ticket with
    ``foreman:impl-review``; the fake dispatcher returns
    ``new_labels={foreman:ready-for-merge}``. Run the iteration twice.
    Iteration 1 dispatches ``RUN_REVIEWER_IMPL``. Iteration 2 must
    dispatch ``MERGE_IMPL_PR`` (when ``auto_merge_impl=True``) — NEVER
    ``RUN_REVIEWER_IMPL`` again.
    """
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()

    def _result(ticket: Ticket, action: Action) -> RoleResult:
        if action.kind == ActionKind.RUN_REVIEWER_IMPL:
            return RoleResult(
                new_labels=frozenset({"foreman:ready-for-merge"}),
                structured_output={"reviewer_outcome": "clean"},
                outcome="success",
            )
        # MERGE_IMPL_PR → empty (issue closed, label removed).
        return RoleResult(
            new_labels=frozenset(),
            structured_output={"merged_impl_pr": 99},
            outcome="success",
        )

    dispatcher = _FakeRoleDispatcher(result_factory=_result)
    # auto_merge_impl=True so the second iteration dispatches MERGE_IMPL_PR
    # rather than parking the ticket on ready-for-merge.
    projects = {
        "voice": ProjectConfig(
            repo="jeffrichley/voice",
            local_clone_path="/tmp/voice",
            apps=AppsConfig(),
            auto_merge_impl=True,
        )
    }

    impl_review_ticket = Ticket(
        project_name="voice",
        issue_number=42,
        labels=frozenset({"foreman:impl-review"}),
        last_transition_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    queue.enqueue(impl_review_ticket)

    # Iteration 1: dispatches RUN_REVIEWER_IMPL on impl-review labels.
    await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )
    assert len(dispatcher.calls) == 1
    _ticket1, action1 = dispatcher.calls[0]
    assert action1.kind == ActionKind.RUN_REVIEWER_IMPL

    # Iteration 2: the next-iteration dispatch decision uses the role's
    # authoritative ``new_labels`` ({foreman:ready-for-merge}), NOT a stale
    # snapshot. Therefore the next action is MERGE_IMPL_PR — not a
    # repeat RUN_REVIEWER_IMPL on the stale impl-review snapshot (the
    # foreman#91 bug).
    await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )
    assert len(dispatcher.calls) == 2
    _ticket2, action2 = dispatcher.calls[1]
    assert action2.kind == ActionKind.MERGE_IMPL_PR, (
        f"Expected MERGE_IMPL_PR on iteration 2, got {action2.kind} — "
        "this would happen if the worker re-dispatched on a stale "
        "impl-review snapshot (foreman#91 regression)."
    )

    # And critically: no call's action is RUN_REVIEWER_IMPL beyond the first.
    later_reviewer_impl = [
        c for c in dispatcher.calls[1:] if c[1].kind == ActionKind.RUN_REVIEWER_IMPL
    ]
    assert later_reviewer_impl == [], (
        "Worker re-dispatched RUN_REVIEWER_IMPL on stale labels — "
        "foreman#91 regression."
    )


@pytest.mark.asyncio
async def test_run_one_iteration_parks_after_reviewer_impl_when_auto_merge_disabled(
    tmp_path: Path,
) -> None:
    """foreman#91 second variant: when ``auto_merge_impl=False``, the
    second iteration finds no action on ``foreman:ready-for-merge`` (the
    project requires a human merge) — it must NOT dispatch
    ``RUN_REVIEWER_IMPL`` again on stale labels.
    """
    storage = Storage(tmp_path / "f.sqlite")
    storage.init()
    queue = DaemonQueue()
    locks = TicketLockManager()

    def _result(ticket: Ticket, action: Action) -> RoleResult:
        return RoleResult(
            new_labels=frozenset({"foreman:ready-for-merge"}),
            structured_output={"reviewer_outcome": "clean"},
            outcome="success",
        )

    dispatcher = _FakeRoleDispatcher(result_factory=_result)
    projects = _project_configs()  # auto_merge_impl=False by default

    impl_review_ticket = Ticket(
        project_name="voice",
        issue_number=42,
        labels=frozenset({"foreman:impl-review"}),
        last_transition_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    queue.enqueue(impl_review_ticket)

    await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0][1].kind == ActionKind.RUN_REVIEWER_IMPL
    # With auto_merge_impl=False, next_action returns None for
    # ready-for-merge → ticket parked, queue empty.
    assert len(queue) == 0

    # Iteration 2 finds nothing to do.
    advanced = await run_one_iteration(
        queue=queue, locks=locks, dispatcher=dispatcher, storage=storage, projects=projects
    )
    assert advanced is False
    # Critically: dispatcher still has only 1 call. No stale-snapshot
    # RUN_REVIEWER_IMPL re-dispatch.
    assert len(dispatcher.calls) == 1
