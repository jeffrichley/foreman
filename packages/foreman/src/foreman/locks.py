"""Per-ticket asyncio locks for the Foreman daemon.

Even in v1 (single worker), the lock pattern is in place so v2 can bump
``max_concurrent_workers`` without restructuring the worker loop. Without
this, two workers could pull the same ticket from the queue and double-
dispatch a role.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class TicketLockManager:
    """Issue an asyncio.Lock per ``(project, issue_number)`` on demand.

    Locks are created lazily and never freed — once a ticket has been
    locked once, its lock object stays in the dict. The footprint is
    small (one Lock per active ticket + tombstones for terminated ones);
    if we ever need to cap memory in long-running daemons we can prune.
    Not a concern for v1's scale.
    """

    def __init__(self) -> None:
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}

    @asynccontextmanager
    async def lock(self, project: str, issue_number: int) -> AsyncIterator[None]:
        key = (project, issue_number)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        async with self._locks[key]:
            yield
