"""Bus endpoint translating subprocess ExecutionLogWrite envelopes into log rows.

Subprocesses (planner/worker/reviewer/fixer) communicate progress over the
agent-core bus instead of writing sqlite directly. This keeps the v3 daemon
the SINGLE writer of execution_log.

Envelope shape (sent via mcp__agent-core__send):

    {
      "kind": "Event",
      "type": "ExecutionLogWrite",
      "data": {
        "ticket_id": "jeffrichley/foreman#143",
        "project": "foreman",
        "action": "worker_heartbeat",
        "outcome": "running",
        "details": {"progress": "8/8 passing"},
        "parent_log_id": null  # or an int for termination rows
      }
    }
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from foreman.reconciler.exec_log import ExecutionLog


class ExecutionLogWritePayload(BaseModel):
    """Pydantic model for the bus envelope's `data` block."""

    ticket_id: str = Field(..., description="Project-qualified issue id, e.g. 'owner/repo#143'")
    project: str = Field(..., description="Local project name, e.g. 'foreman'")
    action: str = Field(..., description="Action name matching the Action enum value")
    outcome: str = Field(..., description="'running' | 'success' | 'error' | 'skipped' | 'dry_run'")
    details: dict[str, Any] = Field(default_factory=dict, description="Free-form structured details")
    parent_log_id: int | None = Field(
        default=None,
        description="If set, this row is a termination of the parent row; daemon will "
        "issue terminate_action() so has_unterminated() returns False after.",
    )
    rule_name: str | None = Field(
        default=None,
        description="Which rule fired this action, if known. NULL for subprocess-internal writes.",
    )


def handle_envelope(payload: ExecutionLogWritePayload, *, log: ExecutionLog) -> int:
    """Translate a validated ExecutionLogWritePayload into an execution_log row.

    Returns the row id of the written row.
    """
    if payload.parent_log_id is not None:
        return log.terminate_action(
            parent_log_id=payload.parent_log_id,
            outcome=payload.outcome,
            details=payload.details,
        )
    return log.write_action(
        ticket_id=payload.ticket_id,
        project=payload.project,
        rule_name=payload.rule_name,
        action=payload.action,
        outcome=payload.outcome,
        details=payload.details,
    )
