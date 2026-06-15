"""transition() publishes the five lifecycle events at the right boundaries."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.event_bus import EventBus
from foreman.v4.events import (
    Event,
    ExecuteCompletedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext, TicketState
from foreman.v4.states.terminal import DoneState, NeedsHelpState


class _ClassicState(TicketState):
    state_name = "Classic"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return None


class _FailEnter(TicketState):
    state_name = "FailEnter"

    def enter(self, ctx: StateContext) -> None:
        raise RuntimeError("enter boom")

    def execute(self, ctx: StateContext) -> Outcome:  # pragma: no cover
        raise NotImplementedError

    def next_state(self, outcome: Outcome) -> TicketState | None:  # pragma: no cover
        return None


@pytest.fixture()
def setup():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Classic", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(received.append)
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
        bus=bus,
    )
    return repo, ticket, instance, received, ctx


def test_happy_path_emits_four_events(setup):
    repo, ticket, instance, received, ctx = setup
    _ClassicState().transition(ctx)
    kinds = [type(ev).__name__ for ev in received]
    assert kinds == [
        "StateEnteredEvent",
        "ExecuteStartedEvent",
        "ExecuteCompletedEvent",
        "StateExitedEvent",
    ]


def test_execute_completed_carries_outcome_and_next_state(setup):
    repo, ticket, instance, received, ctx = setup
    _ClassicState().transition(ctx)
    completed = [ev for ev in received if isinstance(ev, ExecuteCompletedEvent)][0]
    assert completed.outcome.kind == OutcomeKind.CLEAN
    assert completed.next_state == ""  # terminal — no next state


def test_enter_failure_emits_failed_event_no_exit(setup):
    repo, ticket, instance, received, ctx = setup
    _FailEnter().transition(ctx)
    kinds = [type(ev).__name__ for ev in received]
    # enter() raised → no StateEntered, no Execute*, no Exited.
    assert kinds == ["StateFailedEvent"]
    failed = received[0]
    assert isinstance(failed, StateFailedEvent)
    assert failed.failure_phase == "enter"
    assert "enter boom" in failed.failure_reason


def test_no_bus_means_no_events(setup):
    repo, ticket, instance, received, _ctx = setup
    # Rebuild ctx without a bus
    ctx_no_bus = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        bus=None,
    )
    _ClassicState().transition(ctx_no_bus)
    assert received == []  # the original bus saw nothing


def test_misbehaving_observer_does_not_break_transition(setup):
    repo, ticket, instance, _received, ctx = setup

    def boom(_):
        raise RuntimeError("observer boom")

    ctx.bus.subscribe(boom)
    result = _ClassicState().transition(ctx)
    assert result is None  # terminal completion path
    # And the journal row was still finalized:
    closed = repo.get_state_instance(instance.id)
    assert not closed.is_in_flight


# --- Phase 8d.12: terminal-state landing emits StateEnteredEvent ---
#
# Without the fix, transitioning a ticket onto Done / Failed / NeedsHelp
# only flipped tickets.current_state and exited the prior instance — no
# StateEnteredEvent ever fired for the terminal, so
# LabelObservabilityObserver never stamped foreman:state-needshelp (or
# -done / -failed). algokit#21 ended in NeedsHelp with no state label at
# all, hiding the result from anyone viewing the GitHub issue.


class _AdvanceToTerminalState(TicketState):
    """A state whose next_state() returns a configurable terminal.

    Used to exercise the happy-path branch where transition() advances
    the ticket onto a terminal landing.
    """

    state_name = "AdvanceToTerminal"

    def __init__(self, terminal: TicketState) -> None:
        self._terminal = terminal

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="advance",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return self._terminal


def _setup_terminal_advance(terminal: TicketState):
    """Repo + ctx + received-events list wired for a terminal-advance test."""
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(
        project="p", issue_number=1, now=dt.datetime(2026, 6, 15),
    )
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="AdvanceToTerminal", sequence=1,
        now=dt.datetime(2026, 6, 15),
    )
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(received.append)
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 15, 12, 0, 0),
        bus=bus,
    )
    return repo, ticket, received, ctx


def test_transition_to_terminal_state_emits_state_entered_event() -> None:
    """Advancing to NeedsHelp must emit StateEnteredEvent(NeedsHelp).

    Pins the fix for the algokit#21 dogfood gap: without this, the label
    observer never sees the terminal and the GitHub issue ends with no
    foreman:state-* label.
    """
    repo, ticket, received, ctx = _setup_terminal_advance(NeedsHelpState())
    state = _AdvanceToTerminalState(NeedsHelpState())
    result = state.transition(ctx)
    assert result is not None
    assert result.state_name == "NeedsHelp"

    entered_events = [e for e in received if isinstance(e, StateEnteredEvent)]
    # Two StateEnteredEvent fires expected: one for the AdvanceToTerminal
    # state itself (the normal lifecycle event), and one for the
    # terminal landing (the new behavior we're pinning).
    entered_state_names = [e.state_name for e in entered_events]
    assert "NeedsHelp" in entered_state_names, (
        f"expected StateEnteredEvent for NeedsHelp, got: {entered_state_names!r}"
    )

    # The terminal-landing event must carry the freshly opened
    # state_instance row's id / sequence so observers / archive can
    # correlate the event back to a journal row.
    terminal_event = next(e for e in entered_events if e.state_name == "NeedsHelp")
    landing_row = repo.list_state_instances_for_ticket(ticket.id)[-1]
    assert landing_row.state_name == "NeedsHelp"
    assert terminal_event.instance_id == landing_row.id
    assert terminal_event.sequence == landing_row.sequence


def test_transition_to_done_emits_state_entered_event() -> None:
    """Same gap, Done variant — the happy-path completion must also be
    visible to observers as a StateEnteredEvent(Done)."""
    repo, ticket, received, ctx = _setup_terminal_advance(DoneState())
    state = _AdvanceToTerminalState(DoneState())
    state.transition(ctx)

    entered_state_names = [
        e.state_name for e in received if isinstance(e, StateEnteredEvent)
    ]
    assert "Done" in entered_state_names


def test_terminal_landing_emits_no_state_exited_event() -> None:
    """The ticket is permanently parked in the terminal — the observer
    must keep the foreman:state-<terminal> label visible. Emitting
    StateExitedEvent would drive LabelObservabilityObserver to remove
    the label that just got added, defeating the purpose."""
    repo, ticket, received, ctx = _setup_terminal_advance(NeedsHelpState())
    state = _AdvanceToTerminalState(NeedsHelpState())
    state.transition(ctx)

    exited_for_terminal = [
        e for e in received
        if isinstance(e, StateExitedEvent) and e.state_name == "NeedsHelp"
    ]
    assert exited_for_terminal == []


def test_retry_cap_branch_emits_state_entered_for_needs_help() -> None:
    """The runaway-defense branch also transitions to NeedsHelp without
    going through the WorkerPool, so it must synthesize the terminal
    landing event the same way the happy-path does.

    Mirrors test_state_retry_cap_publishes_state_failed_event but
    asserts the StateEntered fix instead of the StateFailed event.
    """
    from foreman.v4.events import StateFailedEvent as _SFE
    from foreman.v4.repository import InMemoryTicketRepository as _Repo
    from foreman.v4.state import StateContext as _Ctx

    repo = _Repo()
    now = dt.datetime(2026, 6, 15, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=2, now=now)
    # Seed 3 consecutive same-state failures so the next attempt trips
    # the cap.
    last_seq = 0
    for seq in range(1, 4):
        inst = repo.open_state_instance(
            ticket_id=ticket.id, state_name="Burner", sequence=seq, now=now,
        )
        repo.record_failure(
            inst.id, now=now, failure_phase="execute",
            failure_reason="role crashed",
        )
        repo.close_state_instance(inst.id, now=now)
        last_seq = seq
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Burner", sequence=last_seq + 1,
        now=now,
    )

    class _Burner(TicketState):
        state_name = "Burner"

        def execute(self, ctx):  # pragma: no cover — cap trips first
            raise NotImplementedError

        def next_state(self, outcome):  # pragma: no cover
            return None

    received: list[Event] = []
    bus = EventBus()
    bus.subscribe(received.append)
    ctx = _Ctx(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: now, bus=bus, max_state_attempts=3,
    )
    result = _Burner().transition(ctx)
    assert result is not None
    assert result.state_name == "NeedsHelp"

    # The cap-trip StateFailedEvent still fires (existing behavior).
    failed = [e for e in received if isinstance(e, _SFE)]
    assert len(failed) == 1
    assert failed[0].failure_phase == "retry_cap"

    # AND the new fix: StateEnteredEvent(NeedsHelp) fires for the
    # terminal landing so the label observer stamps the issue.
    entered_terminals = [
        e for e in received
        if isinstance(e, StateEnteredEvent) and e.state_name == "NeedsHelp"
    ]
    assert len(entered_terminals) == 1, (
        f"expected exactly one StateEnteredEvent(NeedsHelp) on retry-cap "
        f"escalation, got: {[type(e).__name__ + '/' + getattr(e, 'state_name', '') for e in received]!r}"
    )


def test_terminal_landing_state_instance_row_is_closed() -> None:
    """The synthetic terminal row should be created AND closed in the
    same transition. Leaving it in-flight would confuse
    list_in_flight_state_instances() and the WorkerPool's reconciliation."""
    repo, ticket, received, ctx = _setup_terminal_advance(NeedsHelpState())
    state = _AdvanceToTerminalState(NeedsHelpState())
    state.transition(ctx)

    landing_row = repo.list_state_instances_for_ticket(ticket.id)[-1]
    assert landing_row.state_name == "NeedsHelp"
    assert not landing_row.is_in_flight
    assert repo.list_in_flight_state_instances() == []


def test_terminal_landing_no_bus_does_not_crash() -> None:
    """No bus means no event publishes, but the journal row should still
    get created + closed for audit completeness."""
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(
        project="p", issue_number=3, now=dt.datetime(2026, 6, 15),
    )
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="AdvanceToTerminal", sequence=1,
        now=dt.datetime(2026, 6, 15),
    )
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 15, 12, 0, 0),
        bus=None,
    )
    state = _AdvanceToTerminalState(DoneState())
    state.transition(ctx)

    landing_row = repo.list_state_instances_for_ticket(ticket.id)[-1]
    assert landing_row.state_name == "Done"
    assert not landing_row.is_in_flight
