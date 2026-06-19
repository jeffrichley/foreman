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
    def increment(self, name: str, *, tags: dict[str, str]) -> None: ...
    def observe(self, name: str, value: float, *, tags: dict[str, str]) -> None: ...


class NoopMetricsBackend:
    """Default backend — discards everything."""

    def increment(self, name: str, *, tags: dict[str, str]) -> None:
        return None

    def observe(self, name: str, value: float, *, tags: dict[str, str]) -> None:
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
