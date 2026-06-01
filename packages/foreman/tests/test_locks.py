"""Tests for per-ticket asyncio locks."""

from __future__ import annotations

import asyncio

import pytest

from foreman.locks import TicketLockManager


@pytest.mark.asyncio
async def test_acquire_same_ticket_blocks_until_released() -> None:
    mgr = TicketLockManager()
    events: list[str] = []

    async def task_a() -> None:
        async with mgr.lock("voice", 42):
            events.append("a-acquired")
            await asyncio.sleep(0.05)
            events.append("a-releasing")

    async def task_b() -> None:
        await asyncio.sleep(0.01)  # ensure A acquires first
        async with mgr.lock("voice", 42):
            events.append("b-acquired")

    await asyncio.gather(task_a(), task_b())
    assert events == ["a-acquired", "a-releasing", "b-acquired"]


@pytest.mark.asyncio
async def test_different_tickets_do_not_block_each_other() -> None:
    mgr = TicketLockManager()
    events: list[str] = []

    async def task_for(project: str, issue: int, marker: str) -> None:
        async with mgr.lock(project, issue):
            events.append(f"{marker}-in")
            await asyncio.sleep(0.05)
            events.append(f"{marker}-out")

    await asyncio.gather(
        task_for("voice", 42, "a"),
        task_for("voice", 43, "b"),
        task_for("chrona", 42, "c"),
    )
    assert {"a-in", "a-out", "b-in", "b-out", "c-in", "c-out"} == set(events)


@pytest.mark.asyncio
async def test_lock_releases_on_exception() -> None:
    mgr = TicketLockManager()

    async def task_a() -> None:
        async with mgr.lock("voice", 42):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await task_a()

    # Lock should be releasable now; this would deadlock if the prior
    # acquisition leaked.
    async with mgr.lock("voice", 42):
        pass
