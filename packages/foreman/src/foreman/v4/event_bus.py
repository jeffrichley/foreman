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
from collections.abc import Callable

from foreman.v4.events import Event, TicketEvent

_log = logging.getLogger(__name__)

EventListener = Callable[[Event], None]


def _listener_name(listener: EventListener) -> str:
    return getattr(listener, "__name__", type(listener).__name__)


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
            except Exception as exc:
                observer = _listener_name(listener)
                exc_type = type(exc).__name__
                extra = {
                    "observer": observer,
                    "exc_type": exc_type,
                    "exc_message": str(exc),
                }
                if isinstance(event, TicketEvent):
                    # Ticket-scoped events: log the full
                    # ticket-and-instance context so a stuck ticket is
                    # immediately findable in the JSONL.
                    _log.warning(
                        "observer raised on %s for ticket=%d instance=%d: "
                        "observer=%s exc=%s: %s",
                        type(event).__name__,
                        event.ticket_id,
                        event.instance_id,
                        observer,
                        exc_type,
                        exc,
                        exc_info=True,
                        extra=extra,
                    )
                else:
                    # Daemon-level events (BackupTakenEvent /
                    # BackupFailedEvent — issue #360): no ticket /
                    # instance fields. Log a degraded message that
                    # keeps the event type, observer, and exception
                    # context. Without this isinstance guard, the
                    # AttributeError on ``event.ticket_id`` would
                    # bubble out of ``publish`` and crash the daemon
                    # on the first backup event.
                    _log.warning(
                        "observer raised on %s: observer=%s exc=%s: %s",
                        type(event).__name__,
                        observer,
                        exc_type,
                        exc,
                        exc_info=True,
                        extra=extra,
                    )
