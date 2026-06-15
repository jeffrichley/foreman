"""Template Method orchestration — happy path + per-phase failures."""
from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext, TicketState


class _Recorder:
    """Tracks which hooks were invoked, in order."""

    def __init__(self) -> None:
        self.calls: list[str] = []


class _HappyState(TicketState):
    state_name = "Happy"

    def __init__(self, recorder: _Recorder, next_: TicketState | None) -> None:
        self.r = recorder
        self._next = next_

    def enter(self, ctx: StateContext) -> None:
        self.r.calls.append("enter")

    def execute(self, ctx: StateContext) -> Outcome:
        self.r.calls.append("execute")
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        )

    def verify(self, ctx: StateContext, outcome: Outcome) -> None:
        self.r.calls.append("verify")

    def exit(self, ctx: StateContext, outcome: Outcome | None) -> None:
        self.r.calls.append("exit")

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return self._next


class _NextState(TicketState):
    state_name = "Next"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="done",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return None


class _RaisingState(TicketState):
    state_name = "Raising"

    def __init__(self, raise_in: str, recorder: _Recorder) -> None:
        self._raise_in = raise_in
        self.r = recorder

    def enter(self, ctx: StateContext) -> None:
        self.r.calls.append("enter")
        if self._raise_in == "enter":
            raise RuntimeError("enter boom")

    def execute(self, ctx: StateContext) -> Outcome:
        self.r.calls.append("execute")
        if self._raise_in == "execute":
            raise RuntimeError("execute boom")
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        )

    def verify(self, ctx: StateContext, outcome: Outcome) -> None:
        self.r.calls.append("verify")
        if self._raise_in == "verify":
            raise RuntimeError("verify boom")

    def exit(self, ctx: StateContext, outcome: Outcome | None) -> None:
        self.r.calls.append("exit")
        if self._raise_in == "exit":
            raise RuntimeError("exit boom")

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return None


@pytest.fixture()
def setup() -> tuple[InMemoryTicketRepository, Any, Any]:
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Happy", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    return repo, ticket, instance


def _ctx(repo: InMemoryTicketRepository, ticket: Any, instance: Any) -> StateContext:
    return StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
    )


def test_happy_path_invokes_all_five_hooks_in_order(setup: tuple[InMemoryTicketRepository, Any, Any]) -> None:
    repo, ticket, instance = setup
    recorder = _Recorder()
    next_state = _NextState()
    state = _HappyState(recorder, next_state)
    result = state.transition(_ctx(repo, ticket, instance))
    assert recorder.calls == ["enter", "execute", "verify", "exit"]
    assert result is next_state
    closed = repo.get_state_instance(instance.id)
    assert not closed.is_in_flight
    assert closed.outcome_kind == OutcomeKind.CLEAN
    assert closed.next_state == "Next"
    assert repo.get_ticket(ticket.id).current_state == "Next"


def test_can_run_false_records_held_and_returns_none(setup: tuple[InMemoryTicketRepository, Any, Any]) -> None:
    repo, ticket, instance = setup
    repo.hold_ticket(ticket.id, held_by="jeff", reason="vacation", now=dt.datetime(2026, 6, 13))
    held_ticket = repo.get_ticket(ticket.id)
    recorder = _Recorder()
    state = _HappyState(recorder, None)
    result = state.transition(_ctx(repo, held_ticket, instance))
    assert result is None
    assert recorder.calls == []  # no hooks ran
    fetched = repo.get_state_instance(instance.id)
    assert fetched.failure_phase == "can_run"
    assert fetched.failure_reason == "held"
    assert fetched.is_in_flight  # instance NOT closed; transition is no-op


def test_enter_raises_records_failure_and_skips_exit(setup: tuple[InMemoryTicketRepository, Any, Any]) -> None:
    repo, ticket, instance = setup
    recorder = _Recorder()
    state = _RaisingState(raise_in="enter", recorder=recorder)
    result = state.transition(_ctx(repo, ticket, instance))
    assert result is None
    assert recorder.calls == ["enter"]  # exit not called because enter never returned
    fetched = repo.get_state_instance(instance.id)
    assert fetched.failure_phase == "enter"
    assert "enter boom" in (fetched.failure_reason or "")


def test_execute_raises_records_failure_and_calls_exit(setup: tuple[InMemoryTicketRepository, Any, Any]) -> None:
    repo, ticket, instance = setup
    recorder = _Recorder()
    state = _RaisingState(raise_in="execute", recorder=recorder)
    state.transition(_ctx(repo, ticket, instance))
    assert recorder.calls == ["enter", "execute", "exit"]
    fetched = repo.get_state_instance(instance.id)
    assert fetched.failure_phase == "execute"


def test_verify_raises_records_failure_and_calls_exit(setup: tuple[InMemoryTicketRepository, Any, Any]) -> None:
    repo, ticket, instance = setup
    recorder = _Recorder()
    state = _RaisingState(raise_in="verify", recorder=recorder)
    state.transition(_ctx(repo, ticket, instance))
    assert recorder.calls == ["enter", "execute", "verify", "exit"]
    fetched = repo.get_state_instance(instance.id)
    assert fetched.failure_phase == "verify"


def test_exit_raises_records_failure_but_transition_completes(setup: tuple[InMemoryTicketRepository, Any, Any]) -> None:
    repo, ticket, instance = setup
    recorder = _Recorder()
    state = _RaisingState(raise_in="exit", recorder=recorder)
    state.transition(_ctx(repo, ticket, instance))
    assert recorder.calls == ["enter", "execute", "verify", "exit"]
    fetched = repo.get_state_instance(instance.id)
    assert fetched.failure_phase == "exit"
    assert not fetched.is_in_flight  # still closed despite exit raising


# --- Phase 8c.2: state-machine retry cap ---


def _seed_consecutive_failures(
    repo: InMemoryTicketRepository, ticket_id: int, state_name: str, count: int,
) -> int:
    """Open + record-failure + close ``count`` consecutive same-state rows.

    Returns the highest sequence used so the caller can open the next
    attempt at sequence+1 (matching how WorkerPool would have done it
    on a real tick).
    """
    now = dt.datetime(2026, 6, 15, 12, 0, 0)
    last_seq = 0
    for seq in range(1, count + 1):
        inst = repo.open_state_instance(
            ticket_id=ticket_id, state_name=state_name, sequence=seq, now=now,
        )
        repo.record_failure(
            inst.id, now=now, failure_phase="execute",
            failure_reason="role crashed",
        )
        repo.close_state_instance(inst.id, now=now)
        last_seq = seq
    return last_seq


def test_state_retry_cap_escalates_to_needs_help_after_n_failures() -> None:
    """After cap consecutive same-state attempts, the next attempt is
    short-circuited to NeedsHelp without running any lifecycle hooks."""
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 15, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=1, now=now)
    # Seed 3 prior same-state failures.
    last_seq = _seed_consecutive_failures(repo, ticket.id, "Raising", count=3)
    # Open the 4th attempt — this is what WorkerPool would do on the
    # next tick. The cap check should trip immediately.
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Raising", sequence=last_seq + 1,
        now=now,
    )
    held_ticket = repo.get_ticket(ticket.id)
    recorder = _Recorder()
    # State.execute would raise if it ran — but it shouldn't.
    state = _RaisingState(raise_in="execute", recorder=recorder)
    ctx = StateContext(
        ticket=held_ticket, instance=instance, repo=repo,
        clock=lambda: now, max_state_attempts=3,
    )
    result = state.transition(ctx)
    assert result is not None
    assert result.state_name == "NeedsHelp"
    # NO hooks ran — execute() never got a chance.
    assert recorder.calls == []
    # The cap-trip is recorded on the instance row for audit.
    fetched = repo.get_state_instance(instance.id)
    assert fetched.failure_phase == "retry_cap"
    assert "Raising" in (fetched.failure_reason or "")
    assert "4" in (fetched.failure_reason or "")  # cap=3, count=4
    assert not fetched.is_in_flight  # instance closed cleanly
    # Ticket state advanced to NeedsHelp so the next tick picks it up
    # as the terminal-pending-human row.
    assert repo.get_ticket(ticket.id).current_state == "NeedsHelp"


def test_state_retry_cap_resets_after_state_advance() -> None:
    """Counter is CONSECUTIVE, not historical. Insert 3 SpecReview,
    then 1 ImplReview, then 1 SpecReview — the latest run has length 1,
    so the cap (3) is not tripped."""
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 15, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=1, now=now)
    # 3 SpecReview failures.
    _seed_consecutive_failures(repo, ticket.id, "SpecReview", count=3)
    # 1 ImplReview success (just open + close — no failure recorded).
    impl_inst = repo.open_state_instance(
        ticket_id=ticket.id, state_name="ImplReview", sequence=4, now=now,
    )
    repo.close_state_instance(impl_inst.id, now=now)
    # Latest run: 1 SpecReview — well under cap.
    consecutive = repo.count_consecutive_same_state(
        ticket_id=ticket.id, state="SpecReview",
    )
    assert consecutive == 0  # the latest sequence is ImplReview
    # Now open a fresh SpecReview attempt.
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="SpecReview", sequence=5, now=now,
    )
    # Consecutive count from sequence=5 (SpecReview) walking back hits
    # ImplReview at sequence=4 — count is 1, not 4.
    consecutive_after = repo.count_consecutive_same_state(
        ticket_id=ticket.id, state="SpecReview",
    )
    assert consecutive_after == 1
    recorder = _Recorder()
    next_state = _NextState()
    state = _HappyState(recorder, next_state)
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: now, max_state_attempts=3,
    )
    result = state.transition(ctx)
    # Cap NOT tripped — lifecycle ran normally.
    assert result is next_state
    assert recorder.calls == ["enter", "execute", "verify", "exit"]


def test_state_retry_cap_default_max_state_attempts_is_3() -> None:
    """StateContext.max_state_attempts defaults to 3 — match V4Config /
    DaemonConfig default so existing tests that construct StateContext
    without the kwarg behave the same as production."""
    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 15, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=1, now=now)
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Happy", sequence=1, now=now,
    )
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo, clock=lambda: now,
    )
    assert ctx.max_state_attempts == 3


def test_state_retry_cap_publishes_state_failed_event() -> None:
    """The cap-trip emits a StateFailedEvent with failure_phase=retry_cap
    so observers + the jsonl log capture the runaway escalation."""
    from foreman.v4.event_bus import EventBus
    from foreman.v4.events import StateFailedEvent

    repo = InMemoryTicketRepository()
    now = dt.datetime(2026, 6, 15, 12, 0, 0)
    ticket = repo.create_ticket(project="p", issue_number=1, now=now)
    last_seq = _seed_consecutive_failures(
        repo, ticket.id, "Raising", count=3,
    )
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Raising", sequence=last_seq + 1,
        now=now,
    )
    received: list[object] = []
    bus = EventBus()
    bus.subscribe(received.append)
    recorder = _Recorder()
    state = _RaisingState(raise_in="execute", recorder=recorder)
    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: now, bus=bus, max_state_attempts=3,
    )
    state.transition(ctx)
    failed_events = [e for e in received if isinstance(e, StateFailedEvent)]
    assert len(failed_events) == 1
    assert failed_events[0].failure_phase == "retry_cap"
    assert "Raising" in failed_events[0].failure_reason
