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

foreman#251 (Phase 1 of the dispatch-recorder design): the payload's
``usage`` field carries the eight provider-reported cost columns. When
present, ``handle_envelope`` writes them into the new ``execution_log``
cost columns alongside the row. ``usage`` is optional — non-cost
writes (heartbeats, label-only actions) leave it ``None`` and the
existing INSERT shape is unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from foreman.provider import UsageInfo
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
    usage: UsageInfo | None = Field(
        default=None,
        description=(
            "foreman#251 Phase 1: when set, the row carries the eight "
            "provider-reported cost columns (input/output tokens, cache "
            "tokens, total cost, model usage breakdown, duration ms, "
            "num turns). Only the DispatchRecorder's CostSubscriber sets "
            "this; heartbeats and non-cost actions leave it None."
        ),
    )


def _usage_to_columns(usage: UsageInfo) -> dict[str, Any]:
    """Translate a :class:`UsageInfo` into the execution_log cost-column dict.

    foreman#251 (Phase 1): the mapping is single-sourced here so the
    ``write_action`` / ``terminate_action`` signatures don't grow eight
    new kwargs each and a future provider's ``UsageInfo`` extension
    only needs to land in one place.
    """
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "total_cost_usd": usage.total_cost_usd,
        # JSON-encode the per-model breakdown so the column stays a TEXT
        # cell — sqlite has no native dict storage, and downstream
        # tooling can ``json.loads`` on read.
        "model_usage_json": (
            json.dumps(usage.model_usage, sort_keys=True)
            if usage.model_usage is not None
            else None
        ),
        "duration_ms": usage.duration_ms,
        "num_turns": usage.num_turns,
    }


def handle_envelope(payload: ExecutionLogWritePayload, *, log: ExecutionLog) -> int:
    """Translate a validated ExecutionLogWritePayload into an execution_log row.

    Returns the row id of the written row.

    foreman#251 (Phase 1): when ``payload.usage`` is set, the eight cost
    columns are populated on the new row alongside the existing fields.
    Old callers (heartbeats, lifecycle markers) pass no ``usage`` and
    the row's cost columns stay NULL.
    """
    usage_columns = _usage_to_columns(payload.usage) if payload.usage is not None else None

    if payload.parent_log_id is not None:
        return log.terminate_action(
            parent_log_id=payload.parent_log_id,
            outcome=payload.outcome,
            details=payload.details,
            usage_columns=usage_columns,
        )
    return log.write_action(
        ticket_id=payload.ticket_id,
        project=payload.project,
        rule_name=payload.rule_name,
        action=payload.action,
        outcome=payload.outcome,
        details=payload.details,
        usage_columns=usage_columns,
    )
