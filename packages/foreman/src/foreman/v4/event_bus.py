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
    """Synchronous pub/sub hub decoupling transition durability from side effects.

    Observers subscribe once and are notified, in subscription order, of
    every event published thereafter. ``publish`` never propagates an
    observer's exception — see its docstring — so a misbehaving observer
    can never block or crash a state transition.
    """

    def __init__(self) -> None:
        self._subscribers: list[EventListener] = []

    def subscribe(self, listener: EventListener) -> None:
        """Register ``listener`` to receive every event published from now on."""
        self._subscribers.append(listener)

    def unsubscribe(self, listener: EventListener) -> None:
        """Remove ``listener`` from the subscriber list.

        Idempotent: unsubscribing a listener that isn't currently
        registered (never subscribed, or already removed) is a silent
        no-op rather than a raised error.
        """
        try:
            self._subscribers.remove(listener)
        except ValueError:
            # Idempotent — unsubscribing twice is a no-op, not an error.
            pass

    def publish(self, event: Event) -> None:
        """Deliver ``event`` to every subscriber, isolating each from the others' failures.

        Iterates a snapshot of the subscriber list so a listener that
        subscribes or unsubscribes mid-dispatch cannot mutate the list
        out from under the loop. An observer that raises is caught and
        logged — with full ticket/instance context for ticket-scoped
        events — rather than allowed to propagate and abort delivery to
        the remaining observers.
        """
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
                    # Daemon-level events (DaemonEvent subclasses): no
                    # ticket / instance fields. Log a degraded message that
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
