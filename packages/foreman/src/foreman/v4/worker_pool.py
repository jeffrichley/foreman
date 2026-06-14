"""WorkerPool — ThreadPoolExecutor draining QueueManager → transition().

Threading is the right tool for v4: every ticket transition spends most of
its wall-clock waiting on subprocess (role dispatch) or network (GitHub
API). Python's GIL releases on those I/O waits, so N OS threads
genuinely run in parallel.

API shape:
  - tick()  — pulls as many WorkItems as the QM gives, submits each to
              the executor with a done_callback that frees the QM slot.
              Returns the number of WorkItems submitted this tick.
  - shutdown(wait=True) — clean stop; drains in-flight, rejects new submits.

Concurrency invariants (enforced by QM, not here):
  - At most one transition per ticket at a time.
  - At most `max_in_flight` tickets running globally.
  - Held tickets / dep-blocked tickets aren't returned by dequeue().
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import logging
from collections.abc import Callable

from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import GitProvider
from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import TicketRepository
from foreman.v4.role_dispatcher import RoleDispatcher
from foreman.v4.state import StateContext
from foreman.v4.states.registry import build_state
from foreman.v4.work import WorkItem

_LOG = logging.getLogger(__name__)


class WorkerPool:
    def __init__(
        self,
        *,
        repo: TicketRepository,
        qm: QueueManager,
        dispatcher: RoleDispatcher,
        git: GitProvider | None,
        bus: EventBus | None,
        clock: Callable[[], dt.datetime],
    ) -> None:
        self._repo = repo
        self._qm = qm
        self._dispatcher = dispatcher
        self._git = git
        self._bus = bus
        self._clock = clock
        # Single concurrency knob: the pool size = the QM's in-flight cap.
        # Splitting them would let pool < QM silently throttle, or pool > QM
        # waste OS threads. Operators dial ONE number in V4Config.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=qm.max_in_flight, thread_name_prefix="foreman-worker",
        )

    def tick(self) -> int:
        """Submit every dispatchable WorkItem to the executor. Returns count submitted."""
        submitted = 0
        while True:
            item = self._qm.dequeue()
            if item is None:
                return submitted
            future = self._executor.submit(self._run_transition, item)
            # `_item=item` default-arg captures by value — avoids the classic
            # "all lambdas see the last loop iteration" bug. Both callbacks
            # fire on success AND exception:
            #   1. mark_done first so the per-ticket slot frees before logging
            #      takes any time.
            #   2. _log_exception surfaces any worker-thread crash — without
            #      it, exceptions raised in _run_transition (e.g.
            #      TicketNotFoundError, sqlite3.OperationalError) are
            #      silently captured by the future and never reach an
            #      operator, turning a stuck pipeline into guess-and-check.
            def _on_done_mark(
                _f: concurrent.futures.Future, _item: WorkItem = item,
            ) -> None:
                self._qm.mark_done(_item)

            def _on_done_log(
                _f: concurrent.futures.Future, _item: WorkItem = item,
            ) -> None:
                self._log_exception(_f, _item)

            future.add_done_callback(_on_done_mark)
            future.add_done_callback(_on_done_log)
            submitted += 1

    def _log_exception(
        self, future: concurrent.futures.Future, item: WorkItem,
    ) -> None:
        exc = future.exception()
        if exc is not None:
            _LOG.exception(
                "WorkerPool transition raised for ticket %d state=%s",
                item.ticket_id, item.state_name,
                exc_info=exc,
            )

    def _run_transition(self, item: WorkItem) -> None:
        ticket = self._repo.get_ticket(item.ticket_id)
        sequence = self._repo.count_state_instances_for_ticket(item.ticket_id) + 1
        instance = self._repo.open_state_instance(
            ticket_id=item.ticket_id,
            state_name=item.state_name,
            sequence=sequence,
            now=self._clock(),
        )
        state = build_state(item.state_name)
        ctx = StateContext(
            ticket=ticket,
            instance=instance,
            repo=self._repo,
            clock=self._clock,
            bus=self._bus,
            role_dispatcher=self._dispatcher,
            git=self._git,
        )
        state.transition(ctx)

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)
