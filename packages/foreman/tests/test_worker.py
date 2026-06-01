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
