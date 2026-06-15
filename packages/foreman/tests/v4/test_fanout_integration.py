"""Phase 2 completion check — one transition reaches all four observers."""
from __future__ import annotations

import datetime as dt
import json
import logging

from foreman.v4.event_bus import EventBus
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.observers.label_observability import LabelObservabilityObserver
from foreman.v4.observers.metrics import MetricsObserver
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.sqlite_repository import SqliteTicketRepository
from foreman.v4.state import StateContext, TicketState


class _DoneState(TicketState):
    state_name = "Done"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="all set",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return None


class _DemoState(TicketState):
    state_name = "Demo"

    def execute(self, ctx: StateContext) -> Outcome:
        return Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="demo ok",
        )

    def next_state(self, outcome: Outcome) -> TicketState | None:
        return _DoneState()


class _RecordingWriter:
    """Records add_labels + remove_labels calls in arrival order.

    Each entry is ``(op, kwargs)`` where ``op`` is ``"add"`` or
    ``"remove"`` so the test can distinguish which side of a transition
    the call came from."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def add_labels(self, **kwargs: object) -> None:
        self.calls.append(("add", kwargs))

    def remove_labels(self, **kwargs: object) -> None:
        self.calls.append(("remove", kwargs))


def test_one_transition_reaches_all_four_observers(caplog):
    caplog.set_level(logging.INFO, logger="foreman.v4.transitions")
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=1, now=dt.datetime(2026, 6, 13))
    instance = repo.open_state_instance(
        ticket_id=ticket.id, state_name="Demo", sequence=1,
        now=dt.datetime(2026, 6, 13),
    )

    bus = EventBus()
    writer = _RecordingWriter()
    bus.subscribe(StructuredLogObserver(logger_name="foreman.v4.transitions"))
    bus.subscribe(LabelObservabilityObserver(writer=writer, repo=repo))
    bus.subscribe(EventArchiveObserver(conn=repo._conn))
    bus.subscribe(MetricsObserver())

    ctx = StateContext(
        ticket=ticket, instance=instance, repo=repo,
        clock=lambda: dt.datetime(2026, 6, 13, 12, 0, 0),
        bus=bus,
    )
    _DemoState().transition(ctx)

    # 1. Structured-log observer wrote JSON lines:
    log_lines = [r.message for r in caplog.records if "ticket_id" in r.message]
    events_logged = {json.loads(line)["event"] for line in log_lines}
    assert {"state_entered", "execute_started", "execute_completed", "state_exited"} <= events_logged

    # 2. Label observer fired on both lifecycle boundaries: add on
    #    StateEntered (stamps the new state) and remove on StateExited
    #    (cleans up the now-exited state). Trigger label preservation
    #    lives in the dedicated observer tests; here we just verify the
    #    fanout shape.
    assert writer.calls == [
        ("add", {"project": "p", "issue_number": 1,
                 "labels": {"foreman:state-demo"}}),
        ("remove", {"project": "p", "issue_number": 1,
                    "labels": {"foreman:state-demo"}}),
    ]

    # 3. Event-archive observer wrote rows into events table:
    rows = repo._conn.execute(
        "SELECT event_type FROM events ORDER BY id"
    ).fetchall()
    types = [r["event_type"] for r in rows]
    assert types == ["state_entered", "execute_started", "execute_completed", "state_exited"]

    # 4. (Metrics observer is no-op-backed; we just verify it didn't raise.
    #    Recording-backend coverage lives in test_metrics.)
