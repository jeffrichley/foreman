"""TicketState ABC and StateContext shape."""

from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext, TicketState, _enter_terminal
from foreman.v4.states.terminal import DoneState


class _ConcreteState(TicketState):
    state_name = "Concrete"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary="ok",
        )

    def next_state(self, ctx: StateContext, outcome: Outcome) -> TicketState | None:
        return None


def test_ticket_state_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        TicketState()  # type: ignore[abstract]


def test_concrete_state_uses_class_name_default():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Concrete",
        sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    state = _ConcreteState()
    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
    )
    assert state.can_run(ctx) is True  # default True
    assert state.enter(ctx) is None  # default no-op
    outcome = state.execute(ctx)
    assert outcome.kind == OutcomeKind.CLEAN
    state.verify(ctx, outcome)  # default no-op
    state.exit(ctx, outcome)  # default no-op


def test_default_can_run_respects_hold():
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    repo.hold_ticket(ticket.id, held_by="jeff", reason="vacation", now=dt.datetime(2026, 6, 13))
    held_ticket = repo.get_ticket(ticket.id)
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Concrete",
        sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    state = _ConcreteState()
    ctx = StateContext(
        ticket=held_ticket,
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
    )
    assert state.can_run(ctx) is False


def test_enter_terminal_cleans_sandbox_scratch(tmp_path):
    """Landing on a terminal state removes the ticket's per-job scratch dirs."""
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="foreman", issue_number=42, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Merging",
        sequence=1,
        now=dt.datetime(2026, 6, 13),
    )

    scratch_root = tmp_path / ".scratch"
    (scratch_root / "foreman" / "worker-42" / "clone").mkdir(parents=True)

    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        sandbox_scratch_root=scratch_root,
    )
    _enter_terminal(ctx, DoneState())

    assert not (scratch_root / "foreman" / "worker-42").exists()


def test_enter_terminal_noop_when_no_scratch_root(tmp_path):
    """Default (sandbox off / tests): no scratch root → no cleanup, no error."""
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="foreman", issue_number=42, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Merging",
        sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        # sandbox_scratch_root default None
    )
    _enter_terminal(ctx, DoneState())  # must not raise


def test_enter_terminal_swallows_cleanup_error(tmp_path, monkeypatch):
    """A cleanup failure is best-effort: it must not propagate out of _enter_terminal."""
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="foreman", issue_number=42, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id,
        state_name="Merging",
        sequence=1,
        now=dt.datetime(2026, 6, 13),
    )
    ctx = StateContext(
        ticket=ticket,
        instance=instance,
        repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13),
        sandbox_scratch_root=tmp_path / ".scratch",
    )

    def _boom(**kwargs):
        raise OSError("scratch volume unavailable")

    monkeypatch.setattr("foreman.v4.sandbox_clone.cleanup_ticket_scratch", _boom)

    _enter_terminal(ctx, DoneState())  # must not raise despite cleanup blowing up
