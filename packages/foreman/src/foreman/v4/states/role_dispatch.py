"""Base class for the six role-dispatch states.

Subclass: set ``state_name``, ``role``, and override ``next_state_for(outcome)``.
That's it — the dispatch + parse + Outcome plumbing lives here once.

foreman#361: ``next_state`` is a Template Method that intercepts the
``TRANSIENT_PROVIDER_ERROR`` outcome kind before delegating to
``next_state_for``. Concrete subclasses keep overriding
``next_state_for`` only — the transient branch is concentrated here
so all six role-dispatch states get the backoff scheduler + escalation
behavior identically.
"""

from __future__ import annotations

import datetime as dt
from abc import abstractmethod

from foreman.v4.backoff import next_retry_delay
from foreman.v4.events import TransientProviderErrorEvent
from foreman.v4.outcome import Outcome, OutcomeKind, parse_outcome_from_stdout
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.resolve_dispatch import resolve_dispatch


class RoleDispatchState(TicketState):
    """Common ``execute`` + ``next_state`` plumbing for role-dispatching states."""

    role: str = ""  # subclasses MUST override

    def execute(self, ctx: StateContext) -> Outcome:
        if ctx.role_dispatcher is None:
            raise RuntimeError(
                f"{self.state_name}.execute requires a role_dispatcher in StateContext"
            )
        # Crash-recovery resume arm: decide FRESH vs verified-RESUME before
        # dispatching. ``self.role`` already uniquely encodes the work shape
        # (planner / worker / reviewer-spec / reviewer-impl / fixer-spec /
        # fixer-impl), so ``(role=self.role, target=None)`` is a complete,
        # consistent work identity. The same ``(role, target)`` arguments are
        # used here at stamp-time and at the later resume-time — both are this
        # same state class with the same ``self.role`` — so the stamp/resume
        # consistency invariant holds by construction. We deliberately do NOT
        # derive ``target`` separately (that would risk stamp/resume divergence).
        plan = resolve_dispatch(ctx, role=self.role, target=None)
        # Persist the id on THIS instance's row so a future crash re-run can
        # find + verify it. ``execute_started_at`` was already stamped by the
        # transition lifecycle (state.py: mark_execute_started, before this
        # execute() call) — that is the routing signal for the next attempt.
        ctx.repo.set_session_id(ctx.instance.id, plan.session_id)
        stdout = ctx.role_dispatcher.dispatch(
            role=self.role,
            project=ctx.ticket.project,
            issue_number=ctx.ticket.issue_number,
            ticket_id=ctx.ticket.id,
            # foreman#367: thread the state-instance id so the role-core
            # dedup-key construction is stable across retries on the
            # same state instance.
            state_instance_id=ctx.instance.id,
            session_id=plan.session_id,
            resume=plan.resume,
        )
        return parse_outcome_from_stdout(stdout)

    @abstractmethod
    def next_state_for(self, outcome: Outcome) -> TicketState | None:
        """Override per state. Drives the outcome-kind → next-state branching."""

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        """Template Method — intercept TRANSIENT_PROVIDER_ERROR; delegate the rest.

        foreman#361:

        * On ``TRANSIENT_PROVIDER_ERROR``: count consecutive prior
          transients on this ticket via the new repo helper,
          consult :func:`next_retry_delay`. If a delay is returned,
          set ``next_action_at`` on the ticket so the Poller skips
          it until then, publish a
          :class:`TransientProviderErrorEvent`, and return a fresh
          instance of ``self.__class__`` (re-enter the same state,
          no cap-burn). If the schedule has exhausted (None
          returned), publish the same event with
          ``next_retry_at=None`` and route to
          :class:`NeedsHelpState`.
        * On any other outcome: defensively clear
          ``next_action_at`` (a stale suspension MUST NOT outlive a
          successful retry) and delegate to ``next_state_for``.

        Concrete subclasses (Planning, SpecReview, SpecFix,
        Implementing, ImplReview, ImplFix) keep overriding
        ``next_state_for`` only — they are not touched by this
        Template Method addition.
        """
        if outcome.kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR:
            return self._handle_transient(ctx, outcome)
        # foreman#361: defense in depth — any successful retry clears
        # the suspension. The transient branch above sets a fresh
        # next_action_at; this branch ensures a non-transient outcome
        # always leaves the suspension cleared even if the upstream
        # write was missed.
        ctx.repo.clear_next_action_at(ctx.ticket.id)
        return self.next_state_for(outcome)

    def _handle_transient(
        self, ctx: StateContext, outcome: Outcome
    ) -> TicketState | None:
        """Schedule a backoff retry OR escalate to NeedsHelp.

        See :meth:`next_state` for the call-site contract. The
        ``provider_status`` field on the emitted event comes
        verbatim from ``outcome.details["provider_status"]`` so the
        runbook-promised cause-string detail flows end-to-end.
        """
        consecutive = ctx.repo.count_consecutive_transient_provider_errors(
            ctx.ticket.id
        )
        delay = next_retry_delay(consecutive)
        provider_status = ""
        details = outcome.details or {}
        raw_status = details.get("provider_status")
        if isinstance(raw_status, str):
            provider_status = raw_status
        if delay is None:
            # Schedule exhausted — route to NeedsHelp. The terminal
            # landing event is synthesized by transition()'s call
            # site via ``_enter_terminal``; we simply emit the
            # transient-provider event with ``next_retry_at=None``
            # so the structured log captures the escalation moment.
            if ctx.bus is not None:
                ctx.bus.publish(
                    TransientProviderErrorEvent(
                        ticket_id=ctx.ticket.id,
                        instance_id=ctx.instance.id,
                        state_name=ctx.instance.state_name,
                        sequence=ctx.instance.sequence,
                        at=ctx.clock(),
                        attempt=consecutive,
                        next_retry_at=None,
                        provider_status=provider_status,
                    )
                )
            from foreman.v4.states.terminal import NeedsHelpState
            return NeedsHelpState()

        now = ctx.clock()
        retry_at = now + dt.timedelta(seconds=delay)
        ctx.repo.set_next_action_at(ctx.ticket.id, when=retry_at)
        if ctx.bus is not None:
            ctx.bus.publish(
                TransientProviderErrorEvent(
                    ticket_id=ctx.ticket.id,
                    instance_id=ctx.instance.id,
                    state_name=ctx.instance.state_name,
                    sequence=ctx.instance.sequence,
                    at=now,
                    attempt=consecutive,
                    next_retry_at=retry_at,
                    provider_status=provider_status,
                )
            )
        # Re-enter the same state. The runaway-defense cap is
        # exempted via the TRANSIENT_PROVIDER_ERROR skip in
        # count_consecutive_same_state, so this self-loop is safe.
        return self.__class__()
