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
