"""MetricsObserver — no-op stub today, extensible backend Protocol.

The shape is committed to at v4 ship; the backend is not. Wiring a real
Prometheus / StatsD / OTLP exporter later is a one-class swap, no changes
to the EventBus or any other observer.
"""

from __future__ import annotations

from typing import Protocol

from foreman.v4.events import (
    DaemonEvent,
    Event,
    ExecuteCompletedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
    TicketEvent,
)


class MetricsBackend(Protocol):
    """Structural type for a metrics sink: counters plus observations.

    Lets :class:`MetricsObserver` stay decoupled from any concrete metrics
    library — Prometheus, StatsD, OTLP — until one is actually wired in.
    """

    def increment(self, name: str, *, tags: dict[str, str]) -> None:
        """Increment the named counter by one, tagged with ``tags``."""
        ...

    def observe(self, name: str, value: float, *, tags: dict[str, str]) -> None:
        """Record a single ``value`` sample for the named metric, tagged with ``tags``."""
        ...


class NoopMetricsBackend:
    """Default backend — discards everything."""

    def increment(self, name: str, *, tags: dict[str, str]) -> None:
        """Discard the increment; no-op backend has nowhere to send it."""
        return None

    def observe(self, name: str, value: float, *, tags: dict[str, str]) -> None:
        """Discard the observation; no-op backend has nowhere to send it."""
        return None


class MetricsObserver:
    """Translates state-machine events into counter increments.

    The backend is a swappable :class:`MetricsBackend`; today it defaults
    to :class:`NoopMetricsBackend` since v4 ships with the observer shape
    committed but no concrete exporter wired in yet.
    """

    def __init__(self, *, backend: MetricsBackend | None = None) -> None:
        self._backend = backend or NoopMetricsBackend()

    def __call__(self, event: Event) -> None:
        """Increment the counter matching a ticket event's type and state.

        Daemon-level events are skipped since they carry no
        ``state_name`` to tag the metric with.
        """
        # Daemon-level events (:class:`DaemonEvent` subclasses) have no
        # ``state_name`` and no metric semantics under the current
        # MetricsObserver shape. Skip them BEFORE the
        # ``event.state_name`` access below — without this guard a
        # daemon-level event would raise AttributeError on the
        # ``event.state_name`` access.
        if isinstance(event, DaemonEvent):
            return
        assert isinstance(event, TicketEvent), (
            f"unrecognized Event subclass: {type(event).__name__}"
        )
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
