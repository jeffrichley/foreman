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
from foreman.v4.states.merge_queued import MergeQueuedState
from foreman.v4.states.terminal import DoneState, NeedsHelpState


class _ClassicState(TicketState):
    state_name = "Classic"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary="ok",
        )

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        return None


class _FailEnter(TicketState):
    state_name = "FailEnter"

    def enter(self, ctx: StateContext) -> None:
        raise RuntimeError("enter boom")

    def execute(self, ctx: StateContext) -> Outcome:  # pragma: no cover
        raise NotImplementedError

    def next_state(
        self, ctx: StateContext, outcome: Outcome
    ) -> TicketState | None:  # pragma: no cover
        return None


@pytest.fixture()
def setup():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Classic",
        sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(received.append)
    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
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


# Phase 8d.17 / foreman#315: state machine preserves Outcome.details
# end-to-end. The ExecuteCompletedEvent carries the Outcome object
# directly (not a flattened dict), so anything on the role-emitted
# Outcome including details flows through to subscribers like the
# EventArchiveObserver and any audit / forensics consumer.


class _DiagnosticDetailState(TicketState):
    """A test state whose execute() returns an Outcome with details populated.

    Mirrors a role's give-up path: NEEDS_HELP with diagnostic detail
    that operators need to triage. Without Phase 8d.17 this detail was
    dropped at the v3→v4 flattening point.
    """

    state_name = "DiagnosticDetail"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.NEEDS_HELP,
            confidence=OutcomeConfidence.HIGH,
            summary="incomplete (attempt 1)",
            details={
                "work_comment": "could not finish — `check` recipe missing",
                "did_check_pass": False,
                "confidence": "low",
            },
        )

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        return None


def test_execute_completed_event_payload_includes_details(setup):
    """ExecuteCompletedEvent.outcome.details must round-trip from the
    role's emitted Outcome through transition() to subscribers."""
    repo, ticket, instance, received, ctx = setup
    # Swap to a state-instance row for DiagnosticDetail to match what
    # transition() expects (state_name on the row matches the state).
    new_instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="DiagnosticDetail",
        sequence=2,
        now=dt.datetime(2026, 6, 13),
    )
    ctx_diag = StateContext(
        ticket=ticket,
        instance=new_instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
        bus=ctx.bus,
    )
    _DiagnosticDetailState().transition(ctx_diag)
    completed = [ev for ev in received if isinstance(ev, ExecuteCompletedEvent)][0]
    assert completed.outcome.details["did_check_pass"] is False
    assert "missing" in completed.outcome.details["work_comment"]


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
        ticket=ticket,
        instance=instance,
        repo=repo,
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
# LabelObservabilityObserver never stamped foreman:state-needs-help (or
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
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary="advance",
        )

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        return self._terminal


def _setup_terminal_advance(terminal: TicketState):
    """Repo + ctx + received-events list wired for a terminal-advance test."""
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(
        project="p",
        issue_number=1,
        now=dt.datetime(2026, 6, 15),
    )
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="AdvanceToTerminal",
        sequence=1,
        now=dt.datetime(2026, 6, 15),
    )
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(received.append)
    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
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

    entered_state_names = [e.state_name for e in received if isinstance(e, StateEnteredEvent)]
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
        e for e in received if isinstance(e, StateExitedEvent) and e.state_name == "NeedsHelp"
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
            ticket_id=ticket.id,
            state_name="Burner",
            sequence=seq,
            now=now,
        )
        repo.record_failure(
            inst.id,
            now=now,
            failure_phase="execute",
            failure_reason="role crashed",
        )
        repo.close_state_instance(inst.id, now=now)
        last_seq = seq
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Burner",
        sequence=last_seq + 1,
        now=now,
    )

    class _Burner(TicketState):
        state_name = "Burner"

        def execute(self, ctx):  # pragma: no cover — cap trips first
            raise NotImplementedError

        def next_state(self, ctx, outcome):  # pragma: no cover
            return None

    received: list[Event] = []
    bus = EventBus()
    bus.subscribe(received.append)
    ctx = _Ctx(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: now,
        bus=bus,
        max_state_attempts=3,
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
        e for e in received if isinstance(e, StateEnteredEvent) and e.state_name == "NeedsHelp"
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


# --- Phase 8d.15: F2 + F4 — early-return paths must emit StateExitedEvent ---
#
# Before the fix, both early-return branches in transition() skipped the
# try/finally block that emits StateExitedEvent for the failing state.
# LabelObservabilityObserver listens to StateExitedEvent to REMOVE the
# old state's label. Without that event, the GitHub issue ended up with
# both foreman:state-<failed-state> AND foreman:state-needs-help labels
# (retry-cap branch) or with the state label sticking across operator
# holds (held branch).


def test_retry_cap_emits_state_exited_for_failed_state() -> None:
    """F2: when the runaway-defense cap fires, transition() must emit
    StateExitedEvent for the failed state before synthesizing the
    NeedsHelp terminal landing. The event sequence is:

        StateFailedEvent(retry_cap, state=<failed>)
        StateExitedEvent(state=<failed>)
        StateEnteredEvent(state=NeedsHelp)

    Without the StateExitedEvent, LabelObservabilityObserver never sees
    the failed state exit, leaving foreman:state-<failed> stuck on the
    issue alongside the newly-added foreman:state-needs-help label.
    """
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 15, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=4, now=now)
    # Seed 3 consecutive same-state failures so the next attempt trips
    # the cap immediately.
    last_seq = 0
    for seq in range(1, 4):
        inst = repo.open_state_instance(
            ticket_id=ticket.id,
            state_name="Burner",
            sequence=seq,
            now=now,
        )
        repo.record_failure(
            inst.id,
            now=now,
            failure_phase="execute",
            failure_reason="role crashed",
        )
        repo.close_state_instance(inst.id, now=now)
        last_seq = seq
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Burner",
        sequence=last_seq + 1,
        now=now,
    )

    class _Burner(TicketState):
        state_name = "Burner"

        def execute(self, ctx):  # pragma: no cover — cap trips first
            raise NotImplementedError

        def next_state(self, ctx, outcome):  # pragma: no cover
            return None

    received: list[Event] = []
    bus = EventBus()
    bus.subscribe(received.append)
    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: now,
        bus=bus,
        max_state_attempts=3,
    )
    _Burner().transition(ctx)

    # Assert the SEQUENCE: failed → exited(failed) → entered(NeedsHelp).
    summary = [(type(e).__name__, getattr(e, "state_name", "")) for e in received]
    assert ("StateFailedEvent", "Burner") in summary
    assert ("StateExitedEvent", "Burner") in summary, (
        f"missing StateExitedEvent(Burner) — observer cannot remove the "
        f"foreman:state-burner label. Sequence: {summary!r}"
    )
    assert ("StateEnteredEvent", "NeedsHelp") in summary

    # The StateExitedEvent for the failed state must come BEFORE the
    # NeedsHelp terminal landing — otherwise an observer that does a
    # state-snapshot at the StateEntered moment would still see the
    # failed-state label on the issue.
    exited_idx = summary.index(("StateExitedEvent", "Burner"))
    entered_idx = summary.index(("StateEnteredEvent", "NeedsHelp"))
    assert exited_idx < entered_idx, (
        f"StateExitedEvent(Burner) must precede StateEnteredEvent(NeedsHelp); got: {summary!r}"
    )

    # And the StateFailedEvent for the cap-trip must come before the
    # StateExitedEvent — failure is what triggers the exit.
    failed_idx = summary.index(("StateFailedEvent", "Burner"))
    assert failed_idx < exited_idx


def test_held_ticket_emits_state_exited_event() -> None:
    """F4: when can_run()=False (operator hold), transition() must emit
    StateExitedEvent so LabelObservabilityObserver can remove the stuck
    foreman:state-<X> label and reflect the held status correctly."""
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 15, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=5, now=now)
    repo.hold_ticket(ticket.id, held_by="jeff", reason="paused", now=now)
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Classic",
        sequence=1,
        now=now,
    )

    received: list[Event] = []
    bus = EventBus()
    bus.subscribe(received.append)
    held_ticket = repo.get_ticket(ticket.id)
    ctx = StateContext(
        ticket=held_ticket,
        instance=instance,
        repo=repo,
        clock=lambda: now,
        bus=bus,
    )
    _ClassicState().transition(ctx)

    # Two events fire: StateFailedEvent(can_run) then StateExitedEvent.
    kinds = [type(e).__name__ for e in received]
    assert "StateFailedEvent" in kinds
    assert "StateExitedEvent" in kinds, (
        f"missing StateExitedEvent on held-branch — the state label gets "
        f"stuck on the issue. Sequence: {kinds!r}"
    )
    # Failed precedes exited.
    assert kinds.index("StateFailedEvent") < kinds.index("StateExitedEvent")


def test_terminal_landing_no_bus_does_not_crash() -> None:
    """No bus means no event publishes, but the journal row should still
    get created + closed for audit completeness."""
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(
        project="p",
        issue_number=3,
        now=dt.datetime(2026, 6, 15),
    )
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="AdvanceToTerminal",
        sequence=1,
        now=dt.datetime(2026, 6, 15),
    )
    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 15, 12, 0, 0),
        bus=None,
    )
    state = _AdvanceToTerminalState(DoneState())
    state.transition(ctx)

    landing_row = repo.list_state_instances_for_ticket(ticket.id)[-1]
    assert landing_row.state_name == "Done"
    assert not landing_row.is_in_flight


# --- MergeQueued label-stamp fix (foreman#550 follow-up) ---
#
# MergeQueued is excluded from WorkerPool dispatch (the MergeCoordinator
# drains it directly instead — see merge_coordinator.py), so it shares
# the terminal states' gap above: the normal "WorkerPool opens a row +
# transition() emits StateEntered" flow never runs for it either, and
# foreman:state-merge-queued was never stamped. Unlike a true terminal,
# MergeQueued is TRANSIENT: the coordinator moves the ticket elsewhere
# later. These tests pin the ENTRY half of the two-part fix — the EXIT
# half (StateExitedEvent published by MergeCoordinator._route on drain)
# is covered in test_merge_coordinator.py.


def test_transition_to_merge_queued_emits_state_entered_event() -> None:
    """Advancing to MergeQueued must emit StateEnteredEvent(MergeQueued),
    exactly like the terminal-landing fix above, so the label observer
    stamps the merge-queued label the moment a ticket parks there."""
    repo, ticket, received, ctx = _setup_terminal_advance(MergeQueuedState())
    state = _AdvanceToTerminalState(MergeQueuedState())
    result = state.transition(ctx)
    assert result is not None
    assert result.state_name == "MergeQueued"

    entered_events = [e for e in received if isinstance(e, StateEnteredEvent)]
    entered_state_names = [e.state_name for e in entered_events]
    assert "MergeQueued" in entered_state_names, (
        f"expected StateEnteredEvent for MergeQueued, got: {entered_state_names!r}"
    )

    # The synthesized event must carry the freshly opened state_instance
    # row's real id/sequence (not a placeholder) so observers / archive
    # can correlate it back to a journal row.
    merge_queued_event = next(e for e in entered_events if e.state_name == "MergeQueued")
    landing_row = repo.list_state_instances_for_ticket(ticket.id)[-1]
    assert landing_row.state_name == "MergeQueued"
    assert merge_queued_event.instance_id == landing_row.id
    assert merge_queued_event.sequence == landing_row.sequence


def test_merge_queued_entry_row_stays_in_flight() -> None:
    """Unlike a terminal landing (permanently parked — see
    test_terminal_landing_state_instance_row_is_closed, which closes the
    row immediately), MergeQueued is transient: the ticket WILL leave
    later via MergeCoordinator._route. The synthesized row must stay
    OPEN at entry so the coordinator's exit-side fix has a real,
    still-in-flight row to close (with a real instance_id/sequence)
    when the ticket actually drains."""
    repo, ticket, received, ctx = _setup_terminal_advance(MergeQueuedState())
    state = _AdvanceToTerminalState(MergeQueuedState())
    state.transition(ctx)

    landing_row = repo.list_state_instances_for_ticket(ticket.id)[-1]
    assert landing_row.state_name == "MergeQueued"
    assert landing_row.is_in_flight, (
        "MergeQueued's synthesized row must stay open at entry — closing "
        "it immediately (like a terminal) would leave the coordinator's "
        "later drain with no real row to close."
    )


def test_merge_queued_entry_emits_no_state_exited_event() -> None:
    """Entering MergeQueued must not itself emit StateExitedEvent(MergeQueued)
    — that would immediately un-stamp the label the entry just added. The
    real exit fires later, on drain, from MergeCoordinator._route (see
    test_merge_coordinator.py)."""
    repo, ticket, received, ctx = _setup_terminal_advance(MergeQueuedState())
    state = _AdvanceToTerminalState(MergeQueuedState())
    state.transition(ctx)

    exited_for_merge_queued = [
        e for e in received if isinstance(e, StateExitedEvent) and e.state_name == "MergeQueued"
    ]
    assert exited_for_merge_queued == []


def test_defensive_self_loop_does_not_leak_a_second_merge_queued_row() -> None:
    """08d5aab review finding (Important): a mistaken WorkerPool dispatch of
    an already-parked MergeQueued ticket must not leak the real, still-open
    MergeQueued row.

    ``MergeQueuedState.next_state()`` unconditionally self-loops to a fresh
    ``MergeQueuedState()`` (see that class's module docstring — defensive,
    should never fire in production since QueueManager excludes MergeQueued
    from dispatch). If it ever did fire, ``transition()``'s
    ``next_.state_name == "MergeQueued"`` branch would call
    ``_enter_merge_queued`` again. Without the idempotency guard this opens
    a SECOND in-flight MergeQueued row while the REAL row (opened when the
    ticket first legitimately entered MergeQueued) is still open --
    ``MergeCoordinator._exit_merge_queued`` only ever closes the newest
    in-flight row on drain, so the older real row would leak forever.

    Repro: seed the real row directly (bypassing the WorkerPool, which
    would never actually dispatch MergeQueued), then drive
    ``MergeQueuedState().transition()`` against a SECOND, WorkerPool-opened
    instance for the same ticket -- exactly what a mistaken dispatch would
    look like.
    """
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 15, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=9, now=now)
    repo.set_ticket_state(ticket.id, "MergeQueued", now=now)

    # The REAL row: opened when the ticket first legitimately entered
    # MergeQueued (mirrors what _enter_merge_queued does on the happy
    # path). Left open -- nothing has drained the ticket yet.
    real_row = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="MergeQueued",
        sequence=1,
        now=now,
    )
    assert real_row.is_in_flight

    # The MISTAKEN dispatch: WorkerPool opens its own fresh instance
    # before calling transition() -- see WorkerPool._run_transition. This
    # is ctx.instance for the self-loop below.
    dispatched_row = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="MergeQueued",
        sequence=2,
        now=now,
    )
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(received.append)
    ctx = StateContext(
        ticket=repo.get_ticket(ticket.id),
        instance=dispatched_row,
        repo=repo,
        clock=lambda: now,
        bus=bus,
    )

    MergeQueuedState().transition(ctx)

    # No third row: the guard must reuse the real row instead of opening
    # a duplicate.
    all_rows = repo.list_state_instances_for_ticket(ticket.id)
    assert len(all_rows) == 2, f"expected exactly 2 rows (real + dispatched), got: {all_rows!r}"

    # The finally block always closes ctx.instance (the mistakenly
    # dispatched row) -- that part of transition() is unaffected by the
    # guard.
    closed = repo.get_state_instance(dispatched_row.id)
    assert not closed.is_in_flight

    # The REAL row must still be open -- the guard's entire point. A
    # regression here means the coordinator's later drain would close
    # some OTHER row and this one would leak forever.
    still_open = repo.get_state_instance(real_row.id)
    assert still_open.is_in_flight, (
        "the real MergeQueued row must stay open across a defensive "
        "self-loop -- the guard must not close or replace it"
    )

    # transition() itself always publishes one ordinary StateEnteredEvent
    # for whatever instance it was dispatched against (here,
    # dispatched_row) -- that's unrelated to _enter_merge_queued and
    # fires regardless of the guard. The guard's job is narrower: don't
    # let the self-loop's _enter_merge_queued call synthesize a SECOND
    # one for a brand-new row. Exactly one StateEnteredEvent(MergeQueued)
    # total, carrying the dispatched row's own id -- not a freshly
    # opened third row's.
    entered_for_merge_queued = [
        e for e in received if isinstance(e, StateEnteredEvent) and e.state_name == "MergeQueued"
    ]
    assert len(entered_for_merge_queued) == 1, (
        f"expected exactly one StateEnteredEvent(MergeQueued) -- the guard must suppress "
        f"a duplicate from the self-loop's _enter_merge_queued call; got: "
        f"{entered_for_merge_queued!r}"
    )
    assert entered_for_merge_queued[0].instance_id == dispatched_row.id
