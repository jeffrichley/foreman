"""foreman#361: RoleDispatchState Template Method intercepts
``TRANSIENT_PROVIDER_ERROR``, schedules a backoff retry via
``next_action_at``, exempts the runaway-defense cap, emits
``TransientProviderErrorEvent`` per attempt, and escalates to
NeedsHelp once the schedule exhausts.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from foreman.v4.backoff import BACKOFF_SCHEDULE_SECONDS
from foreman.v4.event_bus import EventBus
from foreman.v4.events import Event, TransientProviderErrorEvent
from foreman.v4.outcome import (
    Outcome,
    OutcomeConfidence,
    OutcomeKind,
)
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext
from foreman.v4.states.impl_fix import ImplFixState
from foreman.v4.states.impl_review import ImplReviewState
from foreman.v4.states.implementing import ImplementingState
from foreman.v4.states.planning import PlanningState
from foreman.v4.states.terminal import NeedsHelpState

_CLOCK_BASE = dt.datetime(2026, 6, 20, 12, 0, 0, tzinfo=dt.UTC)


@pytest.fixture()
def fake_clock() -> Any:
    """Frozen clock; tests advance manually by reassigning ``.now``."""

    class _Clock:
        now: dt.datetime = _CLOCK_BASE

        def __call__(self) -> dt.datetime:
            return self.now

    return _Clock()


def _transient_outcome(provider_status: str = "503 Service Unavailable") -> Outcome:
    return Outcome(
        kind=OutcomeKind.TRANSIENT_PROVIDER_ERROR,
        confidence=OutcomeConfidence.HIGH,
        summary=f"provider transient failure: {provider_status}",
        details={
            "provider_status": provider_status,
            "exception_class": "Exception",
        },
    )


def _clean_outcome() -> Outcome:
    return Outcome(
        kind=OutcomeKind.CLEAN,
        confidence=OutcomeConfidence.HIGH,
        summary="ok",
    )


def _record_attempt(
    *,
    repo: InMemoryTicketRepository,
    state_class: type,
    ticket_id: int,
    sequence: int,
    clock: dt.datetime,
    outcome: Outcome | None,
) -> int:
    """Open + complete + close one state_instance row.

    Mirrors the WorkerPool's normal lifecycle but inline so the
    routing test doesn't need a Daemon. Returns the instance id.
    """
    inst = repo.open_state_instance(
        ticket_id=ticket_id,
        state_name=state_class.state_name,
        sequence=sequence,
        now=clock,
    )
    if outcome is not None:
        repo.mark_execute_completed(
            inst.id,
            now=clock,
            outcome_kind=outcome.kind,
            outcome_payload=outcome.model_dump(mode="json"),
            next_state="",
        )
    repo.close_state_instance(inst.id, now=clock)
    return inst.id


def _open_in_flight(
    *,
    repo: InMemoryTicketRepository,
    state_class: type,
    ticket_id: int,
    sequence: int,
    clock: dt.datetime,
):
    """Open a fresh in-flight state_instance row (no outcome yet).

    Mirrors what WorkerPool does at ``worker_pool.py:125`` just
    before invoking ``transition()`` — so when the test calls
    ``next_state(ctx, outcome)``, the helper that walks the journal
    sees the in-flight row with ``outcome_kind=NULL`` (the skip
    that's load-bearing per the spec's CRITICAL acceptance
    criterion).
    """
    return repo.open_state_instance(
        ticket_id=ticket_id,
        state_name=state_class.state_name,
        sequence=sequence,
        now=clock,
    )


def _make_ctx(
    *,
    repo: InMemoryTicketRepository,
    ticket,
    instance,
    clock_callable,
    bus: EventBus,
) -> StateContext:
    return StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=clock_callable,
        bus=bus,
    )


@pytest.mark.parametrize(
    "state_class",
    [PlanningState, ImplementingState, ImplFixState],
)
def test_role_dispatch_transient_advances_full_schedule(state_class, fake_clock) -> None:
    """Three transients in a row advance through the backoff schedule
    (30s, 2m, 10m) — NOT three identical 30s delays. This is the
    regression guard for the NULL-row skip in
    ``count_consecutive_transient_provider_errors``: without that
    skip every call returns 0 and every delay is BACKOFF[0] = 30s.

    Also asserts:

    * ``count_consecutive_same_state`` returns 0 (the runaway-defense
      cap is exempted via the TRANSIENT_PROVIDER_ERROR skip).
    * ``TransientProviderErrorEvent`` fires with the right
      ``attempt`` and ``next_retry_at``.
    * A subsequent CLEAN outcome clears ``next_action_at`` and the
      state advances normally (PlanningState → SpecReview).
    """
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=fake_clock.now)

    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(received.append)

    # Three transient attempts in a row.
    for sequence in (1, 2, 3):
        instance = _open_in_flight(
            repo=repo,
            state_class=state_class,
            ticket_id=ticket.id,
            sequence=sequence,
            clock=fake_clock.now,
        )
        # Re-fetch ticket so ctx sees current state.
        current_ticket = repo.get_ticket(ticket.id)
        ctx = _make_ctx(
            repo=repo,
            ticket=current_ticket,
            instance=instance,
            clock_callable=fake_clock,
            bus=bus,
        )
        next_state = state_class().next_state(ctx, _transient_outcome())
        # Self-loop — same state, new instance.
        assert isinstance(next_state, state_class)
        # Simulate ``mark_execute_completed`` (called by
        # ``transition`` AFTER ``next_state``) so subsequent attempts
        # see this row as a completed transient.
        repo.mark_execute_completed(
            instance.id,
            now=fake_clock.now,
            outcome_kind=OutcomeKind.TRANSIENT_PROVIDER_ERROR,
            outcome_payload=_transient_outcome().model_dump(mode="json"),
            next_state=state_class.state_name,
        )
        repo.close_state_instance(instance.id, now=fake_clock.now)

    # Verify the delays — full schedule advance, NOT three 30s.
    transient_events = [ev for ev in received if isinstance(ev, TransientProviderErrorEvent)]
    assert len(transient_events) == 3
    expected_delays_seconds = list(BACKOFF_SCHEDULE_SECONDS[:3])
    for ev, expected in zip(transient_events, expected_delays_seconds, strict=True):
        assert ev.next_retry_at is not None
        assert (ev.next_retry_at - ev.at).total_seconds() == expected
    # Per-attempt counter is the PRIOR-completed-transient count.
    assert [ev.attempt for ev in transient_events] == [0, 1, 2]

    # Runaway-defense cap exempted.
    assert repo.count_consecutive_same_state(ticket_id=ticket.id, state=state_class.state_name) == 0

    # Now drive a CLEAN outcome and assert next_action_at clears and
    # the state advances to the per-state CLEAN next.
    instance = _open_in_flight(
        repo=repo,
        state_class=state_class,
        ticket_id=ticket.id,
        sequence=4,
        clock=fake_clock.now,
    )
    current_ticket = repo.get_ticket(ticket.id)
    ctx = _make_ctx(
        repo=repo,
        ticket=current_ticket,
        instance=instance,
        clock_callable=fake_clock,
        bus=bus,
    )
    next_state = state_class().next_state(ctx, _clean_outcome())
    # PlanningState → SpecReview, ImplementingState → ImplReview,
    # ImplFixState → ImplReview — pin the expected per-state branch
    # without re-implementing the routing here.
    assert next_state is not None
    expected_clean_target = {
        PlanningState: "SpecReview",
        ImplementingState: "ImplReview",
        ImplFixState: "ImplReview",
    }[state_class]
    assert next_state.state_name == expected_clean_target

    # next_action_at cleared.
    cleared = repo.get_ticket(ticket.id)
    assert cleared.next_action_at is None


def test_role_dispatch_transient_exhausts_to_needs_help(fake_clock) -> None:
    """The schedule exhausts when prior-completed transient count
    reaches len(BACKOFF_SCHEDULE_SECONDS) (== 4). On that attempt,
    ``next_state`` routes to ``NeedsHelpState`` and emits the
    ``TransientProviderErrorEvent`` with ``next_retry_at=None``.

    Per the canonical counting semantics in
    ``count_consecutive_transient_provider_errors``, the in-flight
    row is skipped — so when we drive 5 transients, the 5th call
    sees 4 prior completed transients in the journal and escalates.
    The 1st through 4th calls each see prior counts 0..3 (delays
    30s, 2m, 10m, 30m respectively).
    """
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=fake_clock.now)

    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(received.append)

    state_class = PlanningState

    # Drive 4 transients that schedule. After each, persist the
    # outcome (mirroring transition()'s mark_execute_completed +
    # close_state_instance calls that follow ``next_state``).
    for sequence in (1, 2, 3, 4):
        instance = _open_in_flight(
            repo=repo,
            state_class=state_class,
            ticket_id=ticket.id,
            sequence=sequence,
            clock=fake_clock.now,
        )
        current_ticket = repo.get_ticket(ticket.id)
        ctx = _make_ctx(
            repo=repo,
            ticket=current_ticket,
            instance=instance,
            clock_callable=fake_clock,
            bus=bus,
        )
        next_state = state_class().next_state(ctx, _transient_outcome())
        assert isinstance(next_state, state_class)
        repo.mark_execute_completed(
            instance.id,
            now=fake_clock.now,
            outcome_kind=OutcomeKind.TRANSIENT_PROVIDER_ERROR,
            outcome_payload=_transient_outcome().model_dump(mode="json"),
            next_state=next_state.state_name,
        )
        repo.close_state_instance(instance.id, now=fake_clock.now)

    # Drive the 5th — schedule has now exhausted (prior count == 4).
    instance = _open_in_flight(
        repo=repo,
        state_class=state_class,
        ticket_id=ticket.id,
        sequence=5,
        clock=fake_clock.now,
    )
    current_ticket = repo.get_ticket(ticket.id)
    ctx = _make_ctx(
        repo=repo,
        ticket=current_ticket,
        instance=instance,
        clock_callable=fake_clock,
        bus=bus,
    )
    next_state = state_class().next_state(ctx, _transient_outcome())
    assert isinstance(next_state, NeedsHelpState)

    # The escalation event has next_retry_at=None and attempt=4.
    transient_events = [ev for ev in received if isinstance(ev, TransientProviderErrorEvent)]
    assert transient_events[-1].next_retry_at is None
    assert transient_events[-1].attempt == 4


def test_role_dispatch_clean_clears_stale_next_action_at(fake_clock) -> None:
    """Defense in depth — a non-transient outcome (CLEAN) ALWAYS
    clears ``next_action_at`` even if it was left set by a prior
    suspension and no transient just landed.
    """
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=fake_clock.now)

    # Pre-load a stale suspension.
    repo.set_next_action_at(ticket.id, when=fake_clock.now + dt.timedelta(seconds=600))

    state_class = PlanningState
    instance = _open_in_flight(
        repo=repo,
        state_class=state_class,
        ticket_id=ticket.id,
        sequence=1,
        clock=fake_clock.now,
    )
    current_ticket = repo.get_ticket(ticket.id)
    bus = EventBus()
    ctx = _make_ctx(
        repo=repo,
        ticket=current_ticket,
        instance=instance,
        clock_callable=fake_clock,
        bus=bus,
    )

    state_class().next_state(ctx, _clean_outcome())

    cleared = repo.get_ticket(ticket.id)
    assert cleared.next_action_at is None


def test_impl_review_override_preserves_transient_intercept(fake_clock) -> None:
    """foreman#418: ImplReviewState overrides ``next_state`` to add the
    impl-merge gate on CLEAN. That override must NOT swallow the
    foreman#361 TRANSIENT_PROVIDER_ERROR intercept — every non-CLEAN
    outcome is delegated to ``super().next_state`` (RoleDispatchState),
    where the backoff machinery lives.

    Drive a single TRANSIENT_PROVIDER_ERROR through the override and
    assert the intercept still fires: it re-enters ImplReview (self-loop,
    not Failed/Merging/ImplApproved), schedules ``next_action_at`` so the
    Poller suspends the ticket, and emits ``TransientProviderErrorEvent``.
    """
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=fake_clock.now)

    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(received.append)

    instance = _open_in_flight(
        repo=repo,
        state_class=ImplReviewState,
        ticket_id=ticket.id,
        sequence=1,
        clock=fake_clock.now,
    )
    current_ticket = repo.get_ticket(ticket.id)
    ctx = _make_ctx(
        repo=repo,
        ticket=current_ticket,
        instance=instance,
        clock_callable=fake_clock,
        bus=bus,
    )

    next_state = ImplReviewState().next_state(ctx, _transient_outcome())

    # Self-loop back into ImplReview — the transient intercept re-enters
    # the same state rather than routing to a terminal or merge state.
    assert isinstance(next_state, ImplReviewState)

    # The Poller suspension was scheduled — proving super().next_state's
    # backoff branch ran through the override.
    suspended = repo.get_ticket(ticket.id)
    assert suspended.next_action_at is not None
    expected_delay = BACKOFF_SCHEDULE_SECONDS[0]
    assert (suspended.next_action_at - fake_clock.now).total_seconds() == expected_delay

    # And the structured event fired with a real retry time (attempt 0).
    transient_events = [ev for ev in received if isinstance(ev, TransientProviderErrorEvent)]
    assert len(transient_events) == 1
    assert transient_events[0].next_retry_at is not None
    assert transient_events[0].attempt == 0
