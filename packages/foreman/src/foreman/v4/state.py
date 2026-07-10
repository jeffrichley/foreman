"""TicketState — abstract base for every concrete state in the v4 machine.

The five-hook lifecycle is fixed:

    can_run    — preverify; may the state run right now?
    enter      — setup; record entry, allocate resources
    execute    — do the work; return an Outcome
    verify     — postverify; parse + validate the Outcome
    exit       — teardown; always runs after a successful enter()

The Template Method ``transition()`` orchestrates them in order with
per-phase failure handlers. That lands in Task 1.9.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from foreman.v4.event_bus import EventBus
from foreman.v4.events import (
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)
from foreman.v4.outcome import Outcome
from foreman.v4.records import StateInstanceRecord, TicketRecord
from foreman.v4.repository import TicketRepository
from foreman.v4.role_dispatcher import RoleDispatcher

if TYPE_CHECKING:
    from foreman.v4.config import ProjectConfig
    from foreman.v4.git_provider import GitProvider


@dataclass(frozen=True)
class StateContext:
    """The per-transition handle passed to every lifecycle hook.

    ``max_state_attempts`` is the runaway-defense cap surfaced by
    Phase 8c.2: if the ticket has already failed ``max_state_attempts``
    consecutive times on this state, ``transition()`` short-circuits
    to NeedsHelp instead of running ``execute()`` again. Defaults to 3
    so headless tests + legacy callers that construct StateContext
    without the kwarg keep their previous (effectively unbounded) loop
    semantics intact — three consecutive failures is the same default
    as ``max_fix_attempts`` and ``max_impl_attempts`` (V4Config) and
    DaemonConfig.
    """

    ticket: TicketRecord
    instance: StateInstanceRecord
    repo: TicketRepository
    clock: Callable[[], dt.datetime]
    bus: EventBus | None = None
    role_dispatcher: RoleDispatcher | None = None
    git: GitProvider | None = None
    max_state_attempts: int = 3
    # foreman#357: per-project config map keyed by ``ProjectConfig.name``.
    # MergingState reads ``ProjectConfig.dev_base_branch`` from this map
    # to enforce the impl PR's base ref matches the project's configured
    # dev branch before merging. Default empty dict keeps headless tests
    # (and legacy direct-StateContext constructors) green; production
    # populates the map via ``bootstrap_cli_context`` → ``Daemon`` →
    # ``WorkerPool._run_transition``. When ``ctx.ticket.project`` isn't
    # in the map, MergingState logs a warning and skips the guard so the
    # change stays additive.
    project_configs: dict[str, ProjectConfig] = field(default_factory=dict)


def _publish(ctx: StateContext, event_type: type, **kwargs: Any) -> None:
    """Publish ``event_type`` on ``ctx.bus`` with the common envelope fields.

    No-op when ``ctx.bus is None`` so headless transitions stay green.
    """
    if ctx.bus is None:
        return
    ctx.bus.publish(
        event_type(
            ticket_id=ctx.ticket.id,
            instance_id=ctx.instance.id,
            state_name=ctx.instance.state_name,
            sequence=ctx.instance.sequence,
            at=ctx.clock(),
            **kwargs,
        )
    )


#: State names whose transition() returns ``None`` (no further work). The
#: WorkerPool will not re-enqueue the ticket once it lands here, so the
#: state machine must emit lifecycle events for the terminal landing
#: inline — see ``_enter_terminal``. Mirrors ``poller._TERMINAL_STATES``
#: (the Poller skips polling tickets parked in any of these). Note this
#: is intentionally broader than ``repository._TERMINAL_STATES``
#: ({Done, Failed}) — that constant gates ``list_open_tickets``, which
#: must keep NeedsHelp visible to operators awaiting human action.
#: foreman#443: ``ImplApproved`` was removed — it is now a polling-wait
#: state that re-dispatches each tick until it detects the human merge.
_TERMINAL_STATE_NAMES = frozenset({"Done", "Failed", "NeedsHelp"})


def _enter_terminal(ctx: StateContext, terminal: TicketState) -> None:
    """Create a state_instance row for ``terminal`` and emit ``StateEnteredEvent``.

    Terminal states (Done/Failed/NeedsHelp) have nothing to do — their
    ``execute()`` returns a CLEAN landing outcome and ``next_state()``
    returns ``None``. The WorkerPool therefore never re-enqueues them,
    which means the normal "next tick opens a new instance and emits
    StateEntered" flow never runs for the terminal landing. Without
    this helper, ``LabelObservabilityObserver`` (and any other observer
    listening for ``StateEnteredEvent``) silently misses the terminal,
    leaving the GitHub issue with no ``foreman:state-<terminal>`` label
    — the gap that wedged the 2026-06-15 algokit#21 dogfood.

    Resolution: synthesize the terminal's journal row + lifecycle event
    in the same transition that decided to advance to it. No
    ``StateExitedEvent`` is emitted — the ticket is permanently parked
    here, so the state-progress label should remain on the issue for
    human visibility.
    """
    now = ctx.clock()
    sequence = ctx.repo.count_state_instances_for_ticket(ctx.ticket.id) + 1
    terminal_instance = ctx.repo.open_state_instance(
        ticket_id=ctx.ticket.id,
        state_name=terminal.state_name,
        sequence=sequence,
        now=now,
    )
    if ctx.bus is not None:
        ctx.bus.publish(
            StateEnteredEvent(
                ticket_id=ctx.ticket.id,
                instance_id=terminal_instance.id,
                state_name=terminal.state_name,
                sequence=terminal_instance.sequence,
                at=now,
            )
        )
    # Close the row immediately. The ticket has landed; the row exists
    # for journal completeness, not for further dispatch. We deliberately
    # skip StateExitedEvent so the observer keeps the terminal label
    # visible on the issue.
    ctx.repo.close_state_instance(terminal_instance.id, now=now)


def escalate_to_needs_help(
    ctx: StateContext,
    *,
    failure_phase: str,
    failure_reason: str,
) -> TicketState:
    """Park ``ctx``'s ticket on NeedsHelp without running any further hooks.

    The single escalation path shared by:

    * the **runaway-defense cap** in ``transition()`` (a state that failed
      ``max_state_attempts`` consecutive times), and
    * the **WorkerPool crash-handler** (foreman#454) — a worker-thread future
      that raises *outside* transition()'s per-phase handlers (e.g. a repo
      error in ``next_state`` / ``mark_execute_completed``, or ``build_state``
      raising). Left unhandled, the ticket keeps its non-terminal
      ``current_state`` with ``next_action_at`` NULL, so the Poller re-enqueues
      it every tick forever — a silent infinite loop visible only in logs.

    Sequence (mirrors what the runaway-cap branch did inline):
      1. record the failure on the in-flight ``state_instance`` (audit);
      2. emit ``StateFailedEvent`` (observability);
      3. set the ticket to NeedsHelp;
      4. close the failed instance + emit ``StateExitedEvent`` so the observer
         REMOVES the old ``foreman:state-<X>`` label;
      5. synthesize the terminal landing (``_enter_terminal``) so the observer
         ADDS ``foreman:state-needshelp`` and the Poller stops re-enqueueing.

    Steps 4–5 are idempotent against transition()'s own ``finally`` (which may
    have already closed the row + emitted StateExited before re-raising): the
    repo close is a no-op flip and the observer's label removal swallows a 404.
    Returns the :class:`NeedsHelpState`.
    """
    from foreman.v4.states.terminal import NeedsHelpState

    now = ctx.clock()
    ctx.repo.record_failure(
        ctx.instance.id,
        now=now,
        failure_phase=failure_phase,
        failure_reason=failure_reason,
    )
    _publish(
        ctx,
        StateFailedEvent,
        failure_phase=failure_phase,
        failure_reason=failure_reason,
    )
    needs_help = NeedsHelpState()
    ctx.repo.set_ticket_state(ctx.ticket.id, needs_help.state_name, now=now)
    ctx.repo.close_state_instance(ctx.instance.id, now=now)
    _publish(ctx, StateExitedEvent, outcome=None)
    _enter_terminal(ctx, needs_help)
    return needs_help


class TicketState(ABC):
    """One phase in the ticket's lifecycle.

    Subclasses MUST override ``execute()`` and ``next_state()``. The other
    four hooks have sensible defaults; override only when the state needs
    distinct behavior.
    """

    #: Display name; defaults to the class name minus 'State' suffix.
    state_name: str = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.state_name:
            name = cls.__name__
            cls.state_name = name[:-5] if name.endswith("State") else name

    # --- Lifecycle hooks ---

    def can_run(self, ctx: StateContext) -> bool:
        """Preverify gate. Default: refuse to run if the ticket is held."""
        return not ctx.ticket.is_held

    def enter(self, ctx: StateContext) -> None:
        """Setup. Default: no-op."""
        return None

    @abstractmethod
    def execute(self, ctx: StateContext) -> Outcome:
        """Do the work. Return the Outcome the verify hook will parse."""

    def verify(self, ctx: StateContext, outcome: Outcome) -> None:
        """Postverify the outcome. Default: no-op (raise to reject)."""
        return None

    def exit(self, ctx: StateContext, outcome: Outcome | None) -> None:
        """Teardown. Always runs after a successful enter(). Default: no-op.

        ``outcome`` is None when execute() raised before producing one.
        """
        return None

    # --- Transition policy ---

    @abstractmethod
    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        """Decide what comes next. Return None to halt the state machine.

        The ``ctx`` parameter mirrors the other lifecycle hooks
        (``enter`` / ``execute`` / ``verify`` / ``exit``) and is
        load-bearing for the ``RoleDispatchState`` transient-retry
        intercept (foreman#361). Concrete states that don't need
        ``ctx`` may simply ignore it.
        """

    # --- Template Method ---

    def _close_failed_state(self, ctx: StateContext) -> None:
        """Close the state_instance row and emit StateExitedEvent for an early-exit failure.

        Used when a state failed (or was held) and is exiting WITHOUT
        going through the normal verify/exit phases. Centralizes the
        lifecycle invariants both early-return branches
        in transition() must satisfy (can_run-false / retry-cap-trip).
        Without this:

        * LabelObservabilityObserver listens to StateExitedEvent to REMOVE
          the old foreman:state-<X> label. Skipping the event leaves the
          failed-state label stuck on the issue alongside the new state
          label (Bug F2 — retry-cap branch left BOTH foreman:state-<failed>
          AND foreman:state-needshelp on the GitHub issue).
        * close_state_instance flips exited_at, which drops the row from
          partial index idx_state_instances_inflight. Skipping it leaks
          rows on every poll-while-held cycle (Bug F4 — held tickets
          accumulated orphan rows that ALSO tripped the runaway-defense
          cap and escalated the held ticket to NeedsHelp on resume).
        """
        ctx.repo.close_state_instance(ctx.instance.id, now=ctx.clock())
        _publish(ctx, StateExitedEvent, outcome=None)

    def transition(self, ctx: StateContext) -> TicketState | None:
        """Orchestrate the five-hook lifecycle for one state transition attempt.

        The base class controls the flow (can_run gate, runaway-cap
        check, enter/execute/verify/next_state/exit in order, each phase
        wrapped with failure recording and event publication); subclasses
        control the steps. See the docstring of each hook for what its
        handler does on failure.
        """
        if not self.can_run(ctx):
            ctx.repo.record_failure(
                ctx.instance.id,
                now=ctx.clock(),
                failure_phase="can_run",
                failure_reason="held",
            )
            _publish(ctx, StateFailedEvent, failure_phase="can_run", failure_reason="held")
            # Phase 8d.15 (F4): the held branch must close the state_instance
            # row and emit StateExitedEvent — see _close_failed_state.
            self._close_failed_state(ctx)
            return None

        # Runaway defense (Phase 8c.2). The current row was just opened
        # by the WorkerPool, so the consecutive-count includes this
        # attempt: hitting the cap means we've already burned
        # ``max_state_attempts`` cycles on this state without breaking
        # out. We refuse to re-enter — emit a synthetic state_failed
        # event for observability, record the cap-trip on the
        # state_instances row, force the ticket onto NeedsHelp, and
        # close the instance. The lifecycle hooks (enter/execute/
        # verify/exit) never run — this attempt IS the failure event.
        consecutive = ctx.repo.count_consecutive_same_state(
            ticket_id=ctx.ticket.id,
            state=self.state_name,
        )
        if consecutive >= ctx.max_state_attempts:
            reason = (
                f"state {self.state_name!r} failed {consecutive} consecutive "
                f"times (cap={ctx.max_state_attempts}); escalating to NeedsHelp"
            )
            # Single shared escalation path (also used by the WorkerPool
            # crash-handler, foreman#454): record_failure → StateFailed → set
            # NeedsHelp → close + StateExited (drops the old foreman:state-<X>
            # label) → synthesize the terminal landing (adds
            # foreman:state-needshelp; WorkerPool won't re-enqueue once parked).
            return escalate_to_needs_help(
                ctx,
                failure_phase="retry_cap",
                failure_reason=reason,
            )

        try:
            self.enter(ctx)
        except Exception as exc:
            ctx.repo.record_failure(
                ctx.instance.id,
                now=ctx.clock(),
                failure_phase="enter",
                failure_reason=repr(exc),
            )
            _publish(ctx, StateFailedEvent, failure_phase="enter", failure_reason=repr(exc))
            # Skip exit: enter never returned, so no resources to release.
            return None
        _publish(ctx, StateEnteredEvent)

        outcome: Outcome | None = None
        try:
            ctx.repo.mark_execute_started(ctx.instance.id, now=ctx.clock())
            _publish(ctx, ExecuteStartedEvent)
            try:
                outcome = self.execute(ctx)
            except Exception as exc:
                ctx.repo.record_failure(
                    ctx.instance.id,
                    now=ctx.clock(),
                    failure_phase="execute",
                    failure_reason=repr(exc),
                )
                _publish(ctx, StateFailedEvent, failure_phase="execute", failure_reason=repr(exc))
                return None

            try:
                self.verify(ctx, outcome)
            except Exception as exc:
                ctx.repo.record_failure(
                    ctx.instance.id,
                    now=ctx.clock(),
                    failure_phase="verify",
                    failure_reason=repr(exc),
                )
                _publish(ctx, StateFailedEvent, failure_phase="verify", failure_reason=repr(exc))
                return None

            next_ = self.next_state(ctx, outcome)
            ctx.repo.mark_execute_completed(
                ctx.instance.id,
                now=ctx.clock(),
                outcome_kind=outcome.kind,
                outcome_payload=outcome.model_dump(mode="json"),
                next_state=next_.state_name if next_ is not None else "",
            )
            _publish(
                ctx,
                ExecuteCompletedEvent,
                outcome=outcome,
                next_state=next_.state_name if next_ is not None else "",
            )
            if next_ is not None:
                ctx.repo.set_ticket_state(
                    ctx.ticket.id,
                    next_.state_name,
                    now=ctx.clock(),
                )
                if next_.state_name in _TERMINAL_STATE_NAMES:
                    # WorkerPool won't re-enqueue once parked here, so
                    # the terminal landing event has to be synthesized
                    # inline. See ``_enter_terminal`` for the rationale.
                    _enter_terminal(ctx, next_)
            return next_
        finally:
            try:
                self.exit(ctx, outcome)
            except Exception as exc:
                ctx.repo.record_failure(
                    ctx.instance.id,
                    now=ctx.clock(),
                    failure_phase="exit",
                    failure_reason=repr(exc),
                )
                _publish(ctx, StateFailedEvent, failure_phase="exit", failure_reason=repr(exc))
            ctx.repo.close_state_instance(ctx.instance.id, now=ctx.clock())
            _publish(ctx, StateExitedEvent, outcome=outcome)
