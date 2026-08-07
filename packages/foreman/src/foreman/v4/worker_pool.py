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
from foreman.v4.repository import DrainLeaseActiveError, TicketRepository
from foreman.v4.role_dispatcher import RoleDispatcher
from foreman.v4.state import StateContext, escalate_to_needs_help
from foreman.v4.states.registry import build_state
from foreman.v4.work import WorkItem

if TYPE_CHECKING:
    from pathlib import Path

    from foreman.v4.config import ProjectConfig, ProjectRegistry

_LOG = logging.getLogger(__name__)


class WorkerPool:
    """Drains the QueueManager into a ThreadPoolExecutor running transitions.

    Threads are the right primitive here: every ticket transition spends
    most of its wall-clock blocked on subprocess (role dispatch) or
    network (GitHub API) I/O, and both release the GIL, so N worker
    threads run genuinely in parallel. Concurrency invariants (at most one
    transition per ticket at a time, at most ``max_in_flight`` tickets
    globally) are enforced by the QueueManager, not here.
    """

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
        registry: ProjectRegistry | None = None,
        sandbox_scratch_root: Path | None = None,
    ) -> None:
        self._repo = repo
        self._qm = qm
        self._dispatcher = dispatcher
        self._git = git
        self._bus = bus
        self._clock = clock
        # foreman#556: threaded into every StateContext built by
        # _run_transition so _enter_terminal can reclaim the ticket's
        # per-job sandbox scratch on Done/Failed/NeedsHelp. None (sandbox
        # off, or a test-only WorkerPool(...) construction) keeps cleanup
        # a no-op.
        self._sandbox_scratch_root = sandbox_scratch_root
        # Threaded into every StateContext built by _run_transition so
        # the state machine can enforce the runaway-defense cap (Phase
        # 8c.2). Daemon plumbs DaemonConfig.max_state_attempts here.
        self._max_state_attempts = max_state_attempts
        # issue #503 FIX 1: prefer the shared ``ProjectRegistry`` when the
        # Daemon wires it in.  Holding the registry (not a snapshot of
        # registry.current) means every ``_run_transition`` call reads the
        # live map after a hot-reload, without any per-thread snapshot.
        # The fallback to ``project_configs`` (or an empty dict wrapped in
        # a one-shot registry) keeps direct ``WorkerPool(...)`` test
        # constructions working without change.
        from foreman.v4.config import ProjectRegistry as _ProjectRegistry

        if registry is not None:
            self._registry: ProjectRegistry = registry
        else:
            # Backward-compat: wrap the legacy project_configs dict in a
            # one-shot registry so the rest of the code has a uniform path.
            self._registry = _ProjectRegistry(project_configs or {})
        # Single concurrency knob: the pool size = the QM's in-flight cap.
        # Splitting them would let pool < QM silently throttle, or pool > QM
        # waste OS threads. Operators dial ONE number in V4Config.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=qm.max_in_flight,
            thread_name_prefix="foreman-worker",
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
                _f: concurrent.futures.Future[Any],
                _item: WorkItem = item,
            ) -> None:
                self._qm.mark_done(_item)

            def _on_done_log(
                _f: concurrent.futures.Future[Any],
                _item: WorkItem = item,
            ) -> None:
                self._log_exception(_f, _item)

            future.add_done_callback(_on_done_mark)
            future.add_done_callback(_on_done_log)
            submitted += 1

    def _log_exception(
        self,
        future: concurrent.futures.Future[Any],
        item: WorkItem,
    ) -> None:
        exc = future.exception()
        if exc is not None:
            _LOG.exception(
                "WorkerPool transition raised for ticket %d state=%s",
                item.ticket_id,
                item.state_name,
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
                # FIX 1: read the live map from the registry at transition
                # time so a hot-reload that fires between tick() and this
                # point is immediately visible.  No snapshot needed — readers
                # always see a consistent (non-torn) dict under the GIL.
                project_configs=self._registry.current,
                sandbox_scratch_root=self._sandbox_scratch_root,
            )
            state.transition(ctx)
        except DrainLeaseActiveError:
            # NOT a crash — the drain lease is the guard working on its
            # expected path, and a redeploy is a routine event. Escalating
            # here would park a healthy ticket in NeedsHelp on every
            # redeploy that catches a dispatch attempt, turning something
            # that self-heals today (crash_recovery closes the orphan and
            # the restarted daemon re-drives it — measured 2026-08-07, an
            # interrupted merge completed 18s later untouched) into work
            # that waits for a human.
            #
            # Worse, the documented recovery would be `foreman retry`, which
            # on a NeedsHelp ticket holding an approved green PR can reset it
            # to Planning and orphan the merged spec (agent_core#373 ->
            # duplicate #416, 2026-07-17). ImplApproved is the most frequent
            # dispatcher, so that is the likely shape here, not a corner case.
            #
            # No row was inserted — open_state_instance raises before the
            # INSERT — so there is nothing to close. Leave current_state
            # untouched and return; the Poller re-enqueues on the next tick.
            # Unlike the #454 case below, this loop is bounded and
            # self-clearing: the lease is released at daemon startup, and
            # expires on its TTL even if that never happens.
            _LOG.info(
                "ticket %s: dispatch deferred, drain lease active (redeploy in progress)",
                item.ticket_id,
            )
            return
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
        self,
        item: WorkItem,
        ctx: StateContext | None,
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
                # foreman:state-needs-help). Idempotent against transition()'s
                # own finally if it already closed the row.
                escalate_to_needs_help(
                    ctx,
                    failure_phase="worker_crash",
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
                    item.ticket_id,
                    NeedsHelpState().state_name,
                    now=self._clock(),
                )
        except Exception:
            _LOG.exception(
                "WorkerPool crash-escalation failed for ticket %d state=%s",
                item.ticket_id,
                item.state_name,
            )

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop accepting new submissions.

        When ``wait`` is True (the default), blocks until every in-flight
        transition finishes before returning.
        """
        self._executor.shutdown(wait=wait)
