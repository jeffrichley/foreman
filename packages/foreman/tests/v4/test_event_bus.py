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
    boom_logs = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "observer raised" in r.getMessage()
        and r.exc_info is not None
        and "observer boom" in str(r.exc_info[1])
    ]
    assert len(boom_logs) == 2


def test_subscriber_exception_log_names_observer_and_exception(caplog):
    bus = EventBus()

    class BrokenObserver:
        def __call__(self, _):
            raise ValueError("observer details")

    bus.subscribe(BrokenObserver())
    with caplog.at_level(logging.WARNING, logger="foreman.v4.event_bus"):
        bus.publish(_make_event())

    record = next(
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "observer raised" in r.getMessage()
    )
    assert record.observer == "BrokenObserver"
    assert record.exc_type == "ValueError"
    assert "BrokenObserver" in record.getMessage()
    assert "ValueError: observer details" in record.getMessage()


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
