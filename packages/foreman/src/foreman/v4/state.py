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
from dataclasses import dataclass

from foreman.v4.outcome import Outcome
from foreman.v4.records import StateInstanceRecord, TicketRecord
from foreman.v4.repository import TicketRepository


@dataclass(frozen=True)
class StateContext:
    """The per-transition handle passed to every lifecycle hook."""

    ticket: TicketRecord
    instance: StateInstanceRecord
    repo: TicketRepository
    clock: Callable[[], dt.datetime]


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
    def next_state(self, outcome: Outcome) -> TicketState | None:
        """Decide what comes next. Return None to halt the state machine."""

    # --- Template Method ---

    def transition(self, ctx: StateContext) -> TicketState | None:
        """Orchestrate the five-hook lifecycle. The base class controls the
        flow; subclasses control the steps. See the docstring of each hook
        for what its handler does on failure."""

        if not self.can_run(ctx):
            ctx.repo.record_failure(
                ctx.instance.id, now=ctx.clock(),
                failure_phase="can_run", failure_reason="held",
            )
            return None

        try:
            self.enter(ctx)
        except Exception as exc:
            ctx.repo.record_failure(
                ctx.instance.id, now=ctx.clock(),
                failure_phase="enter", failure_reason=repr(exc),
            )
            # Skip exit: enter never returned, so no resources to release.
            return None

        outcome: Outcome | None = None
        try:
            ctx.repo.mark_execute_started(ctx.instance.id, now=ctx.clock())
            try:
                outcome = self.execute(ctx)
            except Exception as exc:
                ctx.repo.record_failure(
                    ctx.instance.id, now=ctx.clock(),
                    failure_phase="execute", failure_reason=repr(exc),
                )
                return None

            try:
                self.verify(ctx, outcome)
            except Exception as exc:
                ctx.repo.record_failure(
                    ctx.instance.id, now=ctx.clock(),
                    failure_phase="verify", failure_reason=repr(exc),
                )
                return None

            next_ = self.next_state(outcome)
            ctx.repo.mark_execute_completed(
                ctx.instance.id, now=ctx.clock(),
                outcome_kind=outcome.kind,
                outcome_payload=outcome.model_dump(mode="json"),
                next_state=next_.state_name if next_ is not None else "",
            )
            if next_ is not None:
                ctx.repo.set_ticket_state(
                    ctx.ticket.id, next_.state_name, now=ctx.clock(),
                )
            return next_
        finally:
            try:
                self.exit(ctx, outcome)
            except Exception as exc:
                ctx.repo.record_failure(
                    ctx.instance.id, now=ctx.clock(),
                    failure_phase="exit", failure_reason=repr(exc),
                )
            ctx.repo.close_state_instance(ctx.instance.id, now=ctx.clock())
