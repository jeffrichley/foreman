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
from typing import TYPE_CHECKING, Any

from foreman.v4.event_bus import EventBus
from foreman.v4.git_provider import GitProvider
from foreman.v4.queue_manager import QueueManager
from foreman.v4.repository import TicketRepository
from foreman.v4.role_dispatcher import RoleDispatcher
from foreman.v4.state import StateContext, escalate_to_needs_help
from foreman.v4.states.registry import build_state
from foreman.v4.work import WorkItem

if TYPE_CHECKING:
    from foreman.v4.config import ProjectConfig

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
        max_state_attempts: int = 3,
        project_configs: dict[str, ProjectConfig] | None = None,
    ) -> None:
        self._repo = repo
        self._qm = qm
        self._dispatcher = dispatcher
        self._git = git
        self._bus = bus
        self._clock = clock
        # Threaded into every StateContext built by _run_transition so
        # the state machine can enforce the runaway-defense cap (Phase
        # 8c.2). Daemon plumbs DaemonConfig.max_state_attempts here.
        self._max_state_attempts = max_state_attempts
        # foreman#357: per-project ProjectConfig map keyed by name.
        # MergingState's base-ref guard reads ``dev_base_branch`` from
        # the entry for ``ticket.project``. Default empty dict so direct
        # ``WorkerPool(...)`` constructions in tests keep working — the
        # guard short-circuits with a warning when the map is empty.
        self._project_configs: dict[str, ProjectConfig] = project_configs or {}
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
            #      TicketNotFoundError, a psycopg OperationalError) are
            #      silently captured by the future and never reach an
            #      operator, turning a stuck pipeline into guess-and-check.
            def _on_done_mark(
                _f: concurrent.futures.Future[Any], _item: WorkItem = item,
            ) -> None:
                self._qm.mark_done(_item)

            def _on_done_log(
                _f: concurrent.futures.Future[Any], _item: WorkItem = item,
            ) -> None:
                self._log_exception(_f, _item)

            future.add_done_callback(_on_done_mark)
            future.add_done_callback(_on_done_log)
            submitted += 1

    def _log_exception(
        self, future: concurrent.futures.Future[Any], item: WorkItem,
    ) -> None:
        exc = future.exception()
        if exc is not None:
            _LOG.exception(
                "WorkerPool transition raised for ticket %d state=%s",
                item.ticket_id, item.state_name,
                exc_info=exc,
            )

    def _run_transition(self, item: WorkItem) -> None:
        ctx: StateContext | None = None
        try:
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
                max_state_attempts=self._max_state_attempts,
                project_configs=self._project_configs,
            )
            state.transition(ctx)
        except Exception:
            # foreman#454: a transition that raises OUTSIDE transition()'s own
            # per-phase handlers (a repo error in next_state /
            # mark_execute_completed, build_state on an unknown state_name, …)
            # would otherwise leave the ticket in a non-terminal current_state
            # with next_action_at NULL — the Poller re-enqueues it every tick
            # forever, a silent infinite loop with evidence only in logs (the
            # _on_done_log callback). Park it on NeedsHelp so the loop stops and
            # the label observer surfaces it. Re-raise so the done-callback
            # still logs the original stack trace.
            self._escalate_crashed_transition(item, ctx)
            raise

    def _escalate_crashed_transition(
        self, item: WorkItem, ctx: StateContext | None,
    ) -> None:
        """Best-effort park-to-NeedsHelp after a worker-thread crash (#454).

        Never masks the original exception: if the repo/bus is itself the
        failure this also raises, which we swallow + log here so the caller's
        ``raise`` still surfaces the real cause via the done-callback.
        """
        try:
            if ctx is not None:
                # The StateContext exists, so we can land the full terminal
                # sequence (close the failed instance + drop its label, add
                # foreman:state-needshelp). Idempotent against transition()'s
                # own finally if it already closed the row.
                escalate_to_needs_help(
                    ctx, failure_phase="worker_crash",
                    failure_reason="worker-thread transition raised",
                )
            else:
                # Crash before the StateContext was built (get_ticket /
                # open_state_instance / build_state raised): no open instance to
                # land a terminal event on. Floor: park the ticket so the Poller
                # stops re-enqueueing. If the repo is itself broken this raises
                # and is logged below — nothing more we can safely do.
                from foreman.v4.states.terminal import NeedsHelpState
                self._repo.set_ticket_state(
                    item.ticket_id, NeedsHelpState().state_name, now=self._clock(),
                )
        except Exception:
            _LOG.exception(
                "WorkerPool crash-escalation failed for ticket %d state=%s",
                item.ticket_id, item.state_name,
            )

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)
