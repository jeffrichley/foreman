"""StructuredLogObserver — JSON-lines per event into a Python logger."""

from __future__ import annotations

import json
import logging
from typing import Any

from foreman.v4.events import (
    BackupFailedEvent,
    BackupTakenEvent,
    DaemonEvent,
    Event,
    ExecuteCompletedEvent,
    ExecuteStartedEvent,
    StateEnteredEvent,
    StateExitedEvent,
    StateFailedEvent,
    TicketEvent,
    TransientProviderErrorEvent,
)

_EVENT_NAMES: dict[type[Event], tuple[str, int]] = {
    StateEnteredEvent:     ("state_entered", logging.INFO),
    ExecuteStartedEvent:   ("execute_started", logging.INFO),
    ExecuteCompletedEvent: ("execute_completed", logging.INFO),
    StateExitedEvent:      ("state_exited", logging.INFO),
    StateFailedEvent:      ("state_failed", logging.WARNING),
    BackupTakenEvent:      ("backup_taken", logging.INFO),
    BackupFailedEvent:     ("backup_failed", logging.ERROR),
    TransientProviderErrorEvent: ("transient_provider_error", logging.WARNING),
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
        # Issue #360: daemon-level events (DaemonEvent subclasses) have
        # no ticket / instance / state / sequence fields. Emit a
        # minimal envelope keyed on event-type-specific payload
        # branches below. The ticket-scoped envelope (the
        # ``else`` arm) keeps the historic shape unchanged.
        if isinstance(event, DaemonEvent):
            daemon_payload: dict[str, Any] = {
                "event": name,
                "at": event.at.isoformat(),
            }
            if isinstance(event, BackupTakenEvent):
                daemon_payload["path"] = event.path
                daemon_payload["size_bytes"] = event.size_bytes
                daemon_payload["pruned_count"] = event.pruned_count
            elif isinstance(event, BackupFailedEvent):
                daemon_payload["phase"] = event.phase
                daemon_payload["reason"] = event.reason
            self._log.log(level, json.dumps(daemon_payload, sort_keys=True))
            return
        # The remaining shape is ticket-scoped. The substrate only
        # publishes :class:`TicketEvent` or :class:`DaemonEvent`
        # today; an unknown ``Event`` subclass at this point would be
        # a programming error. Assert to narrow for mypy AND fail
        # loud if a future event class forgets to pick a base.
        assert isinstance(event, TicketEvent), (
            f"unrecognized Event subclass: {type(event).__name__}"
        )
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
        elif isinstance(event, TransientProviderErrorEvent):
            # foreman#361: the three fields the runbook promises will
            # appear in transient_provider_error log lines. Without
            # this branch they would be silently dropped — the
            # generic dataclass(...) emit path is intentionally
            # absent in this observer.
            payload["attempt"] = event.attempt
            payload["next_retry_at"] = (
                event.next_retry_at.isoformat() if event.next_retry_at else None
            )
            payload["provider_status"] = event.provider_status
        self._log.log(level, json.dumps(payload, sort_keys=True))
