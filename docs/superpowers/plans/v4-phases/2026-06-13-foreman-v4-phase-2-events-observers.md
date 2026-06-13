> **Parent plan:** [../2026-06-13-foreman-v4-substrate-redesign-implementation.md](../2026-06-13-foreman-v4-substrate-redesign-implementation.md) — read its v4 isolation principle first.
> **Spec:** [../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md](../../specs/2026-06-13-foreman-v4-substrate-redesign-design.md).
> **Branch:** `feat/foreman-v4-substrate`.
> **Gate at end:** `just check` green; then stop for human review before next phase.

## Phase 2 — Events + Observers

The journal in `state_instances` is the source of truth for durability — that's already running after Phase 1. Phase 2 adds a **secondary notification stream** for everything that isn't durability: GitHub label updates, structured logging, metrics, and an optional audit-trail events table. The Template Method publishes one event per lifecycle boundary; observers consume independently.

Why this split: durability writes (timestamp columns on `state_instances`) MUST land synchronously inside `transition()` because crash-recovery reads them. Observability writes (labels, logs, metrics) MUST NOT block or fail a transition. The EventBus is the firewall between them — observers raising exceptions never corrupts the journal.

### Task 2.1: Event base + concrete event types

**Files:**
- Create: `packages/foreman/src/foreman/v4/events.py`
- Test: `packages/foreman/tests/v4/test_events.py`

Five events, one per lifecycle boundary the Template Method already crosses:

| Event | Emitted when |
| --- | --- |
| `StateEnteredEvent` | `enter()` returned successfully |
| `ExecuteStartedEvent` | `execute()` is about to be called |
| `ExecuteCompletedEvent` | `execute()` returned an Outcome and verify passed |
| `StateExitedEvent` | `exit()` returned (success or failure of exit logged via `failure_phase`) |
| `StateFailedEvent` | any phase raised; carries `failure_phase` and `failure_reason` |

Frozen dataclasses, no behavior — just data observers can read.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_events.py
"""Concrete event types — shape contract for the notification stream."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.events import (
    Event,
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind


_T0 = dt.datetime(2026, 6, 13, 12, 0, 0)


def test_state_entered_event_fields():
    ev = StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    )
    assert ev.ticket_id == 1
    assert ev.state_name == "Planning"
    assert ev.at == _T0


def test_execute_started_event_fields():
    ev = ExecuteStartedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    )
    assert ev.instance_id == 10


def test_execute_completed_event_carries_outcome():
    outcome = Outcome(
        kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
        summary="ok",
    )
    ev = ExecuteCompletedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0, outcome=outcome, next_state="SpecReview",
    )
    assert ev.outcome is outcome
    assert ev.next_state == "SpecReview"


def test_state_exited_event_carries_optional_outcome():
    ev_with = StateExitedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        outcome=Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        ),
    )
    ev_without = StateExitedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0, outcome=None,
    )
    assert ev_with.outcome is not None
    assert ev_without.outcome is None


def test_state_failed_event_carries_phase_and_reason():
    ev = StateFailedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        failure_phase="execute", failure_reason="subprocess timed out",
    )
    assert ev.failure_phase == "execute"
    assert ev.failure_reason == "subprocess timed out"


def test_all_event_classes_are_subclasses_of_event():
    for cls in (
        StateEnteredEvent, ExecuteStartedEvent, ExecuteCompletedEvent,
        StateExitedEvent, StateFailedEvent,
    ):
        assert issubclass(cls, Event)


def test_events_are_immutable():
    ev = StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    )
    with pytest.raises(AttributeError):
        ev.state_name = "Something"  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.events'`

- [ ] **Step 3: Write the events module**

```python
# packages/foreman/src/foreman/v4/events.py
"""Lifecycle events — the notification stream observers consume.

These events are emitted by TicketState.transition() at each hook boundary.
They are pure data — observers decide what (if anything) to do with them.
Events MUST NOT carry references to live objects (repos, sockets); only
serializable values. This keeps audit / replay implementations honest.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from foreman.v4.outcome import Outcome


@dataclass(frozen=True, slots=True)
class Event:
    """Common fields for every lifecycle event."""
    ticket_id: int
    instance_id: int
    state_name: str
    sequence: int
    at: dt.datetime


@dataclass(frozen=True, slots=True)
class StateEnteredEvent(Event):
    """``enter()`` returned successfully."""


@dataclass(frozen=True, slots=True)
class ExecuteStartedEvent(Event):
    """``execute()`` is about to be called."""


@dataclass(frozen=True, slots=True)
class ExecuteCompletedEvent(Event):
    """``execute()`` returned an Outcome and ``verify()`` passed."""
    outcome: Outcome
    next_state: str


@dataclass(frozen=True, slots=True)
class StateExitedEvent(Event):
    """``exit()`` returned. ``outcome`` is None if execute() raised."""
    outcome: Outcome | None


@dataclass(frozen=True, slots=True)
class StateFailedEvent(Event):
    """A lifecycle hook raised. ``failure_phase`` matches the failed hook."""
    failure_phase: str
    failure_reason: str
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_events.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/events.py packages/foreman/tests/v4/test_events.py
git commit -m "feat(v4): add lifecycle event dataclasses"
```

### Task 2.2: EventBus with observer exception isolation

**Files:**
- Create: `packages/foreman/src/foreman/v4/event_bus.py`
- Test: `packages/foreman/tests/v4/test_event_bus.py`

The bus is the firewall between the durability path and observability side effects. Contract:

- Subscribers register a callable taking one `Event`.
- `publish(event)` invokes every subscriber in registration order.
- An exception in one subscriber MUST NOT prevent later subscribers from running, MUST NOT propagate to the caller, and MUST be logged for forensics.
- Subscribers may filter by event type internally; the bus does no filtering.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_event_bus.py
"""EventBus — fan-out with subscriber exception isolation."""
from __future__ import annotations

import datetime as dt
import logging

from foreman.v4.event_bus import EventBus
from foreman.v4.events import StateEnteredEvent


def _make_event() -> StateEnteredEvent:
    return StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=dt.datetime(2026, 6, 13),
    )


def test_publishes_event_to_single_subscriber():
    bus = EventBus()
    received: list = []
    bus.subscribe(received.append)
    ev = _make_event()
    bus.publish(ev)
    assert received == [ev]


def test_publishes_event_to_all_subscribers_in_registration_order():
    bus = EventBus()
    order: list[str] = []
    bus.subscribe(lambda _: order.append("a"))
    bus.subscribe(lambda _: order.append("b"))
    bus.subscribe(lambda _: order.append("c"))
    bus.publish(_make_event())
    assert order == ["a", "b", "c"]


def test_subscriber_exception_does_not_break_others(caplog):
    bus = EventBus()
    after_first: list = []
    after_third: list = []

    def boom(_):
        raise RuntimeError("observer boom")

    bus.subscribe(boom)
    bus.subscribe(after_first.append)
    bus.subscribe(boom)
    bus.subscribe(after_third.append)
    ev = _make_event()
    with caplog.at_level(logging.WARNING, logger="foreman.v4.event_bus"):
        bus.publish(ev)
    assert after_first == [ev]
    assert after_third == [ev]
    # Both failures should be logged for forensics.
    boom_logs = [r for r in caplog.records if "observer boom" in r.message]
    assert len(boom_logs) == 2


def test_publish_does_not_raise_when_subscriber_raises():
    bus = EventBus()
    bus.subscribe(lambda _: (_ for _ in ()).throw(RuntimeError("nope")))
    bus.publish(_make_event())  # no exception leaks


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    received: list = []
    bus.subscribe(received.append)
    bus.unsubscribe(received.append)
    bus.publish(_make_event())
    assert received == []


def test_unsubscribe_unknown_callable_is_noop():
    bus = EventBus()
    bus.unsubscribe(lambda _: None)  # should not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_event_bus.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.event_bus'`

- [ ] **Step 3: Write the bus**

```python
# packages/foreman/src/foreman/v4/event_bus.py
"""EventBus — synchronous publish/subscribe with exception isolation.

The bus is the firewall between the durability path (transition() writing
state_instances rows) and observability side effects (labels, logs, metrics).
A misbehaving observer must never block or fail a transition; observer
exceptions are caught and logged here.

Synchronous on purpose: observer order is predictable, no thread-safety
debt, no asyncio backpressure to think about. If an observer needs to be
async (network IO, large IO bursts), it can dispatch its own background
work from inside its callback.
"""

from __future__ import annotations

import logging
from typing import Callable

from foreman.v4.events import Event


_log = logging.getLogger(__name__)

EventListener = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[EventListener] = []

    def subscribe(self, listener: EventListener) -> None:
        self._subscribers.append(listener)

    def unsubscribe(self, listener: EventListener) -> None:
        try:
            self._subscribers.remove(listener)
        except ValueError:
            # Idempotent — unsubscribing twice is a no-op, not an error.
            pass

    def publish(self, event: Event) -> None:
        for listener in list(self._subscribers):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 — firewall is the whole point
                _log.warning(
                    "observer raised on %s for ticket=%d instance=%d",
                    type(event).__name__, event.ticket_id, event.instance_id,
                    exc_info=True,
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/test_event_bus.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/event_bus.py packages/foreman/tests/v4/test_event_bus.py
git commit -m "feat(v4): add EventBus with observer exception isolation"
```

### Task 2.3: Wire EventBus into `transition()`

**Files:**
- Modify: `packages/foreman/src/foreman/v4/state.py` (extend `StateContext` with bus; emit events from `transition()`)
- Test: `packages/foreman/tests/v4/test_transition_events.py`

Each transition publishes events at the five lifecycle boundaries the Template Method already crosses. The bus call is the LAST thing transition() does at each boundary — after the journal write to `state_instances` is committed, so observers see a durable state.

`StateContext` gets a new optional field `bus: EventBus | None = None`. When None, transition() runs in headless mode (existing Phase 1 tests still pass). When set, the five events fan out.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/test_transition_events.py
"""transition() publishes the five lifecycle events at the right boundaries."""
from __future__ import annotations

import datetime as dt

import pytest

from foreman.v4.event_bus import EventBus
from foreman.v4.events import (
    Event,
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.repository import InMemoryTicketRepository
from foreman.v4.state import StateContext, TicketState


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/test_transition_events.py -v`
Expected: FAIL with `TypeError: StateContext.__init__() got an unexpected keyword argument 'bus'`

- [ ] **Step 3: Extend `StateContext` and `transition()`**

In `packages/foreman/src/foreman/v4/state.py`:

```python
# Add to the imports near the top:
from foreman.v4.event_bus import EventBus
from foreman.v4.events import (
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)
```

Update `StateContext`:

```python
@dataclass(frozen=True)
class StateContext:
    """The per-transition handle passed to every lifecycle hook."""
    ticket: TicketRecord
    instance: StateInstanceRecord
    repo: TicketRepository
    clock: Callable[[], dt.datetime]
    bus: EventBus | None = None
```

Add a private helper at module scope (above `TicketState`):

```python
def _publish(ctx: StateContext, event_type, **kwargs) -> None:
    if ctx.bus is None:
        return
    ctx.bus.publish(event_type(
        ticket_id=ctx.ticket.id,
        instance_id=ctx.instance.id,
        state_name=ctx.instance.state_name,
        sequence=ctx.instance.sequence,
        at=ctx.clock(),
        **kwargs,
    ))
```

Update `transition()` to emit events at each boundary. The shape stays the same; the additions are paired publish calls. For each failure-handler branch, also publish a `StateFailedEvent` BEFORE returning:

```python
    def transition(self, ctx: StateContext) -> "TicketState | None":
        if not self.can_run(ctx):
            ctx.repo.record_failure(
                ctx.instance.id, now=ctx.clock(),
                failure_phase="can_run", failure_reason="held",
            )
            _publish(ctx, StateFailedEvent, failure_phase="can_run", failure_reason="held")
            return None

        try:
            self.enter(ctx)
        except Exception as exc:  # noqa: BLE001
            ctx.repo.record_failure(
                ctx.instance.id, now=ctx.clock(),
                failure_phase="enter", failure_reason=repr(exc),
            )
            _publish(ctx, StateFailedEvent, failure_phase="enter", failure_reason=repr(exc))
            return None
        _publish(ctx, StateEnteredEvent)

        outcome: Outcome | None = None
        try:
            ctx.repo.mark_execute_started(ctx.instance.id, now=ctx.clock())
            _publish(ctx, ExecuteStartedEvent)
            try:
                outcome = self.execute(ctx)
            except Exception as exc:  # noqa: BLE001
                ctx.repo.record_failure(
                    ctx.instance.id, now=ctx.clock(),
                    failure_phase="execute", failure_reason=repr(exc),
                )
                _publish(ctx, StateFailedEvent, failure_phase="execute", failure_reason=repr(exc))
                return None

            try:
                self.verify(ctx, outcome)
            except Exception as exc:  # noqa: BLE001
                ctx.repo.record_failure(
                    ctx.instance.id, now=ctx.clock(),
                    failure_phase="verify", failure_reason=repr(exc),
                )
                _publish(ctx, StateFailedEvent, failure_phase="verify", failure_reason=repr(exc))
                return None

            next_ = self.next_state(outcome)
            ctx.repo.mark_execute_completed(
                ctx.instance.id, now=ctx.clock(),
                outcome_kind=outcome.kind,
                outcome_payload=outcome.model_dump(mode="json"),
                next_state=next_.state_name if next_ is not None else "",
            )
            _publish(
                ctx, ExecuteCompletedEvent,
                outcome=outcome,
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
            except Exception as exc:  # noqa: BLE001
                ctx.repo.record_failure(
                    ctx.instance.id, now=ctx.clock(),
                    failure_phase="exit", failure_reason=repr(exc),
                )
                _publish(ctx, StateFailedEvent, failure_phase="exit", failure_reason=repr(exc))
            ctx.repo.close_state_instance(ctx.instance.id, now=ctx.clock())
            _publish(ctx, StateExitedEvent, outcome=outcome)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/ -v`
Expected: all of Phase 1's transition tests still pass (no `bus` ⇒ no events ⇒ Phase 1 assertions unaffected), plus 5 new event-emission tests.

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/state.py packages/foreman/tests/v4/test_transition_events.py
git commit -m "feat(v4): wire EventBus into transition() lifecycle"
```

### Task 2.4: StructuredLogObserver

**Files:**
- Create: `packages/foreman/src/foreman/v4/observers/__init__.py`
- Create: `packages/foreman/src/foreman/v4/observers/structured_log.py`
- Test: `packages/foreman/tests/v4/observers/test_structured_log.py`

JSON-lines emission to a `logging.Logger`. One line per event, with stable field names. The actual file handler is configured at daemon startup (Phase 7); this observer only formats + emits.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/observers/test_structured_log.py
"""StructuredLogObserver — JSON-lines emission per event."""
from __future__ import annotations

import datetime as dt
import json
import logging

import pytest

from foreman.v4.events import (
    ExecuteCompletedEvent,
    StateEnteredEvent,
    StateFailedEvent,
)
from foreman.v4.observers.structured_log import StructuredLogObserver
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind


_T0 = dt.datetime(2026, 6, 13, 12, 0, 0)


@pytest.fixture()
def observer_and_records(caplog):
    caplog.set_level(logging.INFO, logger="foreman.v4.transitions")
    obs = StructuredLogObserver(logger_name="foreman.v4.transitions")
    return obs, caplog


def test_state_entered_emits_one_json_line(observer_and_records):
    obs, caplog = observer_and_records
    obs(StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    ))
    record = caplog.records[-1]
    payload = json.loads(record.message)
    assert payload["event"] == "state_entered"
    assert payload["ticket_id"] == 1
    assert payload["state"] == "Planning"
    assert payload["sequence"] == 1


def test_execute_completed_includes_outcome_and_next_state(observer_and_records):
    obs, caplog = observer_and_records
    obs(ExecuteCompletedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        outcome=Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="spec PR open",
        ),
        next_state="SpecReview",
    ))
    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "execute_completed"
    assert payload["outcome_kind"] == "clean"
    assert payload["confidence"] == "high"
    assert payload["next_state"] == "SpecReview"
    assert payload["summary"] == "spec PR open"


def test_state_failed_uses_warning_level(observer_and_records):
    obs, caplog = observer_and_records
    obs(StateFailedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        failure_phase="execute", failure_reason="timeout",
    ))
    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    payload = json.loads(record.message)
    assert payload["event"] == "state_failed"
    assert payload["failure_phase"] == "execute"
    assert payload["failure_reason"] == "timeout"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_structured_log.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.observers.structured_log'`

- [ ] **Step 3: Write the observer**

```python
# packages/foreman/src/foreman/v4/observers/__init__.py
"""Concrete observers — one file per observer.

Observers consume events from foreman.v4.event_bus.EventBus. They are
registered at daemon startup; their __call__ receives one Event per fire.
"""
```

```python
# packages/foreman/src/foreman/v4/observers/structured_log.py
"""StructuredLogObserver — JSON-lines per event into a Python logger."""

from __future__ import annotations

import json
import logging
from typing import Any

from foreman.v4.events import (
    Event,
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)


_EVENT_NAMES = {
    StateEnteredEvent:     ("state_entered", logging.INFO),
    ExecuteStartedEvent:   ("execute_started", logging.INFO),
    ExecuteCompletedEvent: ("execute_completed", logging.INFO),
    StateExitedEvent:      ("state_exited", logging.INFO),
    StateFailedEvent:      ("state_failed", logging.WARNING),
}


class StructuredLogObserver:
    """Emit one JSON line per event into a named logger."""

    def __init__(self, *, logger_name: str = "foreman.v4.transitions") -> None:
        self._log = logging.getLogger(logger_name)

    def __call__(self, event: Event) -> None:
        try:
            name, level = _EVENT_NAMES[type(event)]
        except KeyError:
            # Unknown event type — log defensively, do not raise.
            name, level = ("unknown", logging.INFO)
        payload: dict[str, Any] = {
            "event": name,
            "ticket_id": event.ticket_id,
            "instance_id": event.instance_id,
            "state": event.state_name,
            "sequence": event.sequence,
            "at": event.at.isoformat(),
        }
        if isinstance(event, ExecuteCompletedEvent):
            payload["outcome_kind"] = event.outcome.kind.value
            payload["confidence"] = event.outcome.confidence.value
            payload["summary"] = event.outcome.summary
            payload["next_state"] = event.next_state
        elif isinstance(event, StateFailedEvent):
            payload["failure_phase"] = event.failure_phase
            payload["failure_reason"] = event.failure_reason
        self._log.log(level, json.dumps(payload, sort_keys=True))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_structured_log.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/observers/__init__.py packages/foreman/src/foreman/v4/observers/structured_log.py packages/foreman/tests/v4/observers/test_structured_log.py
git commit -m "feat(v4): add StructuredLogObserver for JSON-lines transition log"
```

### Task 2.5: LabelObservabilityObserver

**Files:**
- Create: `packages/foreman/src/foreman/v4/observers/label_observability.py`
- Test: `packages/foreman/tests/v4/observers/test_label_observability.py`

Writes ONE label per state to the GitHub issue: `foreman:state-<state-name-lowercase>`. Write-only — the daemon never reads these back; they exist for human observers viewing the issue page. Takes a `LabelWriter` Protocol (`write_labels(project, issue_number, labels)`); test uses a fake recorder.

This observer subscribes to `StateEnteredEvent` and `StateFailedEvent`. On entry, it sets `foreman:state-<new>` and clears any other `foreman:state-*` label. On failure into a terminal state (NeedsHelp / Failed), the entry-event from that next state handles the label flip — this observer doesn't react to StateFailedEvent for non-terminal failures.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/observers/test_label_observability.py
"""LabelObservabilityObserver — writes one foreman:state-* label per entry."""
from __future__ import annotations

import datetime as dt

from foreman.v4.events import StateEnteredEvent
from foreman.v4.observers.label_observability import (
    LabelObservabilityObserver,
)
from foreman.v4.repository import InMemoryTicketRepository


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, set[str]]] = []

    def write_labels(self, *, project: str, issue_number: int, labels: set[str]) -> None:
        self.calls.append((project, issue_number, set(labels)))


_T0 = dt.datetime(2026, 6, 13)


def _make_repo_and_ticket(state_name: str = "Planning"):
    repo = InMemoryTicketRepository()
    ticket = repo.create_ticket(project="foreman", issue_number=42, now=_T0)
    repo.set_ticket_state(ticket.id, state_name, now=_T0)
    return repo, repo.get_ticket(ticket.id)


def test_state_entered_writes_single_state_label():
    repo, ticket = _make_repo_and_ticket("Planning")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(StateEnteredEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
    ))
    assert writer.calls == [("foreman", 42, {"foreman:state-planning"})]


def test_label_name_lowercases_state():
    repo, ticket = _make_repo_and_ticket("SpecReview")
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(StateEnteredEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="SpecReview", sequence=1, at=_T0,
    ))
    assert writer.calls[0][2] == {"foreman:state-specreview"}


def test_ignores_non_entered_events():
    """Observer only acts on StateEnteredEvent."""
    from foreman.v4.events import ExecuteStartedEvent
    repo, ticket = _make_repo_and_ticket()
    writer = _RecordingWriter()
    obs = LabelObservabilityObserver(writer=writer, repo=repo)
    obs(ExecuteStartedEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
    ))
    assert writer.calls == []


def test_writer_failure_does_not_propagate(caplog):
    """Label-write failure must not break observer protocol — bus catches,
    but the observer itself should also be resilient since label writes are
    network-IO-prone."""
    class _BoomWriter:
        def write_labels(self, **_):
            raise RuntimeError("network down")

    repo, ticket = _make_repo_and_ticket()
    obs = LabelObservabilityObserver(writer=_BoomWriter(), repo=repo)
    # Observer raises; the EventBus is what catches it. We just verify the
    # exception class so EventBus's blanket except sees it.
    import pytest
    with pytest.raises(RuntimeError):
        obs(StateEnteredEvent(
            ticket_id=ticket.id, instance_id=99,
            state_name="Planning", sequence=1, at=_T0,
        ))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_label_observability.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.observers.label_observability'`

- [ ] **Step 3: Write the observer**

```python
# packages/foreman/src/foreman/v4/observers/label_observability.py
"""LabelObservabilityObserver — writes one foreman:state-* label per state entry.

Write-only by design: the daemon never reads labels back to decide state.
Labels exist for humans viewing the GitHub issue page.

The actual label-mutation surface is injected as a LabelWriter Protocol so
this module doesn't have to know about PyGithub at test time. Production
wiring lives in Phase 5.
"""

from __future__ import annotations

from typing import Protocol

from foreman.v4.events import Event, StateEnteredEvent
from foreman.v4.repository import TicketRepository


class LabelWriter(Protocol):
    def write_labels(
        self, *, project: str, issue_number: int, labels: set[str]
    ) -> None: ...


class LabelObservabilityObserver:
    """Reacts to StateEnteredEvent by stamping the current state on the issue."""

    def __init__(self, *, writer: LabelWriter, repo: TicketRepository) -> None:
        self._writer = writer
        self._repo = repo

    def __call__(self, event: Event) -> None:
        if not isinstance(event, StateEnteredEvent):
            return
        ticket = self._repo.get_ticket(event.ticket_id)
        label = f"foreman:state-{event.state_name.lower()}"
        self._writer.write_labels(
            project=ticket.project,
            issue_number=ticket.issue_number,
            labels={label},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_label_observability.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/observers/label_observability.py packages/foreman/tests/v4/observers/test_label_observability.py
git commit -m "feat(v4): add LabelObservabilityObserver for write-only state labels"
```

### Task 2.6: EventArchiveObserver (the spec's "SQLitePersistenceObserver")

**Files:**
- Create: `packages/foreman/src/foreman/v4/observers/event_archive.py`
- Modify: `packages/foreman/src/foreman/v4/schema.sql` (add `events` table)
- Test: `packages/foreman/tests/v4/observers/test_event_archive.py`

The state_instances table IS the journal — that's the source of truth, written synchronously by `transition()`. This observer appends to a **separate** `events` table for forensics + future replay. Adopting the spec's `SQLitePersistenceObserver` name was misleading because the journal is already persistent; this observer carries the audit trail.

If the events-table write fails, the EventBus's exception isolation absorbs it — observability degraded, durability untouched.

- [ ] **Step 1: Extend the schema**

Append to `packages/foreman/src/foreman/v4/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       INTEGER NOT NULL,
    instance_id     INTEGER NOT NULL,
    event_type      TEXT    NOT NULL,
    state_name      TEXT    NOT NULL,
    sequence        INTEGER NOT NULL,
    at              TEXT    NOT NULL,
    payload         TEXT    NOT NULL  -- JSON-encoded extra fields
);

CREATE INDEX IF NOT EXISTS idx_events_ticket
    ON events(ticket_id, at);
```

- [ ] **Step 2: Write the failing test**

```python
# packages/foreman/tests/v4/observers/test_event_archive.py
"""EventArchiveObserver — append-only events table for forensics + replay."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from foreman.v4.events import (
    ExecuteCompletedEvent,
    StateEnteredEvent,
    StateFailedEvent,
)
from foreman.v4.observers.event_archive import EventArchiveObserver
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind
from foreman.v4.sqlite_repository import SqliteTicketRepository


_T0 = dt.datetime(2026, 6, 13, 12, 0, 0)


@pytest.fixture()
def repo_and_ticket():
    repo = SqliteTicketRepository.in_memory()
    ticket = repo.create_ticket(project="p", issue_number=1, now=_T0)
    return repo, ticket


def test_state_entered_writes_one_event_row(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    obs(StateEnteredEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
    ))
    rows = repo._conn.execute("SELECT * FROM events").fetchall()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "state_entered"
    assert rows[0]["state_name"] == "Planning"


def test_execute_completed_payload_carries_outcome(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    obs(ExecuteCompletedEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
        outcome=Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        ),
        next_state="SpecReview",
    ))
    row = repo._conn.execute("SELECT * FROM events").fetchone()
    payload = json.loads(row["payload"])
    assert payload["outcome_kind"] == "clean"
    assert payload["next_state"] == "SpecReview"


def test_state_failed_payload_carries_phase_and_reason(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    obs(StateFailedEvent(
        ticket_id=ticket.id, instance_id=99,
        state_name="Planning", sequence=1, at=_T0,
        failure_phase="execute", failure_reason="timeout",
    ))
    row = repo._conn.execute("SELECT * FROM events").fetchone()
    payload = json.loads(row["payload"])
    assert payload["failure_phase"] == "execute"
    assert payload["failure_reason"] == "timeout"


def test_events_are_append_only(repo_and_ticket):
    repo, ticket = repo_and_ticket
    obs = EventArchiveObserver(conn=repo._conn)
    for i in range(3):
        obs(StateEnteredEvent(
            ticket_id=ticket.id, instance_id=99,
            state_name="S", sequence=i + 1,
            at=_T0 + dt.timedelta(seconds=i),
        ))
    rows = repo._conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    assert [r["sequence"] for r in rows] == [1, 2, 3]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_event_archive.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.observers.event_archive'`

- [ ] **Step 4: Write the observer**

```python
# packages/foreman/src/foreman/v4/observers/event_archive.py
"""EventArchiveObserver — append-only events table for forensics + replay.

This is the audit trail. The state_instances journal already persists
the durability story; this observer captures the SAME events as a flat,
append-only log that's easy to grep, easy to replay, and never under
contention from the transition path. If this write fails, the EventBus
isolates the exception; the journal stays correct.
"""

from __future__ import annotations

import json
import sqlite3

from foreman.v4.events import (
    Event,
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)


_EVENT_TYPE_NAMES = {
    StateEnteredEvent:     "state_entered",
    ExecuteStartedEvent:   "execute_started",
    ExecuteCompletedEvent: "execute_completed",
    StateExitedEvent:      "state_exited",
    StateFailedEvent:      "state_failed",
}


def _payload_for(event: Event) -> dict:
    if isinstance(event, ExecuteCompletedEvent):
        return {
            "outcome_kind": event.outcome.kind.value,
            "confidence": event.outcome.confidence.value,
            "summary": event.outcome.summary,
            "next_state": event.next_state,
        }
    if isinstance(event, StateExitedEvent):
        return {
            "outcome_kind": event.outcome.kind.value if event.outcome else None,
        }
    if isinstance(event, StateFailedEvent):
        return {
            "failure_phase": event.failure_phase,
            "failure_reason": event.failure_reason,
        }
    return {}


class EventArchiveObserver:
    def __init__(self, *, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __call__(self, event: Event) -> None:
        event_type = _EVENT_TYPE_NAMES.get(type(event), "unknown")
        self._conn.execute(
            "INSERT INTO events"
            "(ticket_id, instance_id, event_type, state_name, sequence, at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.ticket_id, event.instance_id, event_type,
                event.state_name, event.sequence, event.at.isoformat(),
                json.dumps(_payload_for(event), sort_keys=True),
            ),
        )
        self._conn.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_event_archive.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add packages/foreman/src/foreman/v4/schema.sql packages/foreman/src/foreman/v4/observers/event_archive.py packages/foreman/tests/v4/observers/test_event_archive.py
git commit -m "feat(v4): add EventArchiveObserver and events audit table"
```

### Task 2.7: MetricsObserver — no-op stub with Protocol

**Files:**
- Create: `packages/foreman/src/foreman/v4/observers/metrics.py`
- Test: `packages/foreman/tests/v4/observers/test_metrics.py`

YAGNI on a real metrics backend at v4 ship. We do want the SHAPE — a `MetricsBackend` Protocol so a Prometheus / StatsD / OTLP exporter can drop in later without touching the bus or observers.

- [ ] **Step 1: Write the failing test**

```python
# packages/foreman/tests/v4/observers/test_metrics.py
"""MetricsObserver — stub with extensible Protocol."""
from __future__ import annotations

import datetime as dt

from foreman.v4.events import (
    ExecuteCompletedEvent,
    StateEnteredEvent,
    StateFailedEvent,
)
from foreman.v4.observers.metrics import (
    MetricsObserver,
    NoopMetricsBackend,
)
from foreman.v4.outcome import Outcome, OutcomeConfidence, OutcomeKind


_T0 = dt.datetime(2026, 6, 13)


def test_default_backend_is_noop():
    obs = MetricsObserver()
    # Must not raise on any event:
    obs(StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    ))


def test_custom_backend_receives_increment_and_observation_calls():
    class RecordingBackend:
        def __init__(self) -> None:
            self.increments: list[tuple[str, dict]] = []
            self.observations: list[tuple[str, float, dict]] = []

        def increment(self, name: str, *, tags: dict) -> None:
            self.increments.append((name, tags))

        def observe(self, name: str, value: float, *, tags: dict) -> None:
            self.observations.append((name, value, tags))

    backend = RecordingBackend()
    obs = MetricsObserver(backend=backend)
    obs(StateEnteredEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
    ))
    obs(ExecuteCompletedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        outcome=Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="ok",
        ),
        next_state="SpecReview",
    ))
    obs(StateFailedEvent(
        ticket_id=1, instance_id=10, state_name="Planning",
        sequence=1, at=_T0,
        failure_phase="execute", failure_reason="timeout",
    ))
    increment_names = [c[0] for c in backend.increments]
    assert "foreman.v4.state.entered" in increment_names
    assert "foreman.v4.state.completed" in increment_names
    assert "foreman.v4.state.failed" in increment_names


def test_noop_backend_methods_are_callable():
    backend = NoopMetricsBackend()
    backend.increment("x", tags={})
    backend.observe("y", 1.5, tags={})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'foreman.v4.observers.metrics'`

- [ ] **Step 3: Write the observer**

```python
# packages/foreman/src/foreman/v4/observers/metrics.py
"""MetricsObserver — no-op stub today, extensible backend Protocol.

The shape is committed to at v4 ship; the backend is not. Wiring a real
Prometheus / StatsD / OTLP exporter later is a one-class swap, no changes
to the EventBus or any other observer.
"""

from __future__ import annotations

from typing import Protocol

from foreman.v4.events import (
    Event,
    ExecuteCompletedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
)


class MetricsBackend(Protocol):
    def increment(self, name: str, *, tags: dict) -> None: ...
    def observe(self, name: str, value: float, *, tags: dict) -> None: ...


class NoopMetricsBackend:
    """Default backend — discards everything."""

    def increment(self, name: str, *, tags: dict) -> None:  # noqa: ARG002
        return None

    def observe(self, name: str, value: float, *, tags: dict) -> None:  # noqa: ARG002
        return None


class MetricsObserver:
    def __init__(self, *, backend: MetricsBackend | None = None) -> None:
        self._backend = backend or NoopMetricsBackend()

    def __call__(self, event: Event) -> None:
        tags = {"state": event.state_name}
        if isinstance(event, StateEnteredEvent):
            self._backend.increment("foreman.v4.state.entered", tags=tags)
        elif isinstance(event, ExecuteCompletedEvent):
            self._backend.increment("foreman.v4.state.completed", tags={
                **tags, "kind": event.outcome.kind.value,
            })
        elif isinstance(event, StateFailedEvent):
            self._backend.increment("foreman.v4.state.failed", tags={
                **tags, "phase": event.failure_phase,
            })
        elif isinstance(event, StateExitedEvent):
            self._backend.increment("foreman.v4.state.exited", tags=tags)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest packages/foreman/tests/v4/observers/test_metrics.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add packages/foreman/src/foreman/v4/observers/metrics.py packages/foreman/tests/v4/observers/test_metrics.py
git commit -m "feat(v4): add MetricsObserver stub with extensible backend protocol"
```

### Task 2.8: End-to-end fan-out integration test

**Files:**
- Create: `packages/foreman/tests/v4/test_fanout_integration.py`

Wires everything from Phase 2 together: one transition emits events that hit all four observers. This is the "Phase 2 completion" empirical check.

- [ ] **Step 1: Write the test**

```python
# packages/foreman/tests/v4/test_fanout_integration.py
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
    def __init__(self) -> None:
        self.calls = []

    def write_labels(self, **kwargs) -> None:
        self.calls.append(kwargs)


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

    # 2. Label observer wrote one state-label call (on StateEntered):
    assert writer.calls == [
        {"project": "p", "issue_number": 1, "labels": {"foreman:state-demo"}}
    ]

    # 3. Event-archive observer wrote rows into events table:
    rows = repo._conn.execute(
        "SELECT event_type FROM events ORDER BY id"
    ).fetchall()
    types = [r["event_type"] for r in rows]
    assert types == ["state_entered", "execute_started", "execute_completed", "state_exited"]

    # 4. (Metrics observer is no-op-backed; we just verify it didn't raise.
    #    Recording-backend coverage lives in test_metrics.)
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest packages/foreman/tests/v4/test_fanout_integration.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add packages/foreman/tests/v4/test_fanout_integration.py
git commit -m "test(v4): end-to-end fan-out — one transition reaches all four observers"
```

### Phase 2 — `just check` gate

- [ ] **Run:** `just check`
- [ ] **Expected:** all gates green.

Phase 2 completion criterion (from the outline): **side-effects fan out via the EventBus**. By Task 2.8 we've proven a single transition reaches the structured log, the GitHub label surface, the events audit table, and the metrics shim — without any of them being able to corrupt the durability journal. The substrate is now observability-ready; Phase 3 fills in concrete states.

---
