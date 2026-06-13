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

_EVENT_NAMES: dict[type[Event], tuple[str, int]] = {
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
