"""Daemon worker — runs one ticket through one pipeline stage at a time.

``run_one_iteration`` is the unit-testable heart: pull one ticket, dispatch
one action, persist results. The async ``Worker`` class wraps that in a
loop for the live daemon (added in Phase 8 — daemon composition).
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from foreman.config import ProjectConfig
from foreman.dispatcher import Action, ActionKind, Ticket, next_action
from foreman.locks import TicketLockManager
from foreman.queue import DaemonQueue
from foreman.storage import Storage

_log = logging.getLogger("foreman.daemon.worker")


@dataclass(frozen=True)
class RoleResult:
    """What a role returns after running.

    ``new_labels`` is the post-run label set — what the role applied to
    the issue / PR via the GitHostProvider. The worker re-enqueues the
    ticket with these labels (if there's still actionable work).
    """

    new_labels: frozenset[str]
    structured_output: dict | None
    outcome: str


class RoleDispatcher(Protocol):
    """Abstract interface for dispatching to a specific role.

    The real implementation (added in Phase 10) routes to the existing
    ``run_planner`` / ``run_reviewer`` / ``run_fixer`` / ``run_worker``
    functions. Tests use a fake that returns canned RoleResults.
    """

    async def dispatch(self, *, ticket: Ticket, action: Action) -> RoleResult: ...


_ACTION_TO_ROLE_NAME: dict[ActionKind, str] = {
    ActionKind.RUN_PLANNER: "planner",
    ActionKind.RUN_REVIEWER_SPEC: "reviewer",
    ActionKind.RUN_REVIEWER_IMPL: "reviewer",
    ActionKind.RUN_FIXER_SPEC: "fixer",
    ActionKind.RUN_FIXER_IMPL: "fixer",
    ActionKind.RUN_WORKER: "worker",
    ActionKind.MERGE_SPEC_PR: "daemon",
    ActionKind.MERGE_IMPL_PR: "daemon",
}


async def run_one_iteration(
    *,
    queue: DaemonQueue,
    locks: TicketLockManager,
    dispatcher: RoleDispatcher,
    storage: Storage,
    projects: dict[str, ProjectConfig],
) -> bool:
    """Run one stage on the next actionable ticket. Returns False if queue empty.

    Sequence:
    1. Dequeue the highest-priority actionable ticket
    2. Acquire its per-ticket lock
    3. Compute the action (defensive: labels may have changed between enqueue and now)
    4. Persist node_run start
    5. Dispatch
    6. On success: persist node_run finish + transition; self-notify
        re-enqueue if there's more work
    7. On failure: persist failure row; do NOT advance label (operator
        inspects the foreman:failed marker added by reconciliation later)
    """
    ticket = queue.dequeue(projects)
    if ticket is None:
        return False

    project_cfg = projects[ticket.project_name]
    async with locks.lock(ticket.project_name, ticket.issue_number):
        now = datetime.now(UTC)

        action = next_action(ticket, project_cfg)
        if action is None:
            _log.info(
                "dequeued ticket parked (no actionable next state)",
                extra={
                    "project": ticket.project_name,
                    "issue": ticket.issue_number,
                    "labels": sorted(ticket.labels),
                },
            )
            return True

        _log.info(
            "dispatching role",
            extra={
                "project": ticket.project_name,
                "issue": ticket.issue_number,
                "action": action.kind.value,
                "role": _ACTION_TO_ROLE_NAME[action.kind],
                "labels": sorted(ticket.labels),
            },
        )

        pipeline_id = storage.upsert_pipeline(
            project=ticket.project_name,
            issue_number=ticket.issue_number,
            current_state=",".join(sorted(ticket.labels)),
            started_at=now,
        )

        role_name = _ACTION_TO_ROLE_NAME[action.kind]
        run_id = storage.record_node_run_start(
            pipeline_id=pipeline_id,
            role=role_name,
            identity=f"foreman-{role_name}-bot",
            at=now,
        )

        try:
            result = await dispatcher.dispatch(ticket=ticket, action=action)
        except Exception as exc:
            finish_at = datetime.now(UTC)
            duration_ms = int((finish_at - now).total_seconds() * 1000)
            storage.record_node_run_finish(
                run_id=run_id,
                at=finish_at,
                outcome="failure",
                structured_output=None,
            )
            storage.record_failure(
                pipeline_id=pipeline_id,
                at=finish_at,
                role=role_name,
                reason=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            )
            _log.error(
                "role dispatch failed",
                extra={
                    "project": ticket.project_name,
                    "issue": ticket.issue_number,
                    "role": role_name,
                    "duration_ms": duration_ms,
                    "error_class": type(exc).__name__,
                    "error_message": str(exc),
                },
                exc_info=True,
            )
            return True

        finish_at = datetime.now(UTC)
        storage.record_node_run_finish(
            run_id=run_id,
            at=finish_at,
            outcome=result.outcome,
            structured_output=result.structured_output,
        )
        storage.record_transition(
            pipeline_id=pipeline_id,
            at=finish_at,
            from_labels=sorted(ticket.labels),
            to_labels=sorted(result.new_labels),
            actor=role_name,
        )

        duration_ms = int((finish_at - now).total_seconds() * 1000)
        _log.info(
            "role dispatch completed",
            extra={
                "project": ticket.project_name,
                "issue": ticket.issue_number,
                "role": role_name,
                "outcome": result.outcome,
                "duration_ms": duration_ms,
                "from_labels": sorted(ticket.labels),
                "to_labels": sorted(result.new_labels),
            },
        )

        new_ticket = Ticket(
            project_name=ticket.project_name,
            issue_number=ticket.issue_number,
            labels=result.new_labels,
            last_transition_at=finish_at,
        )
        if next_action(new_ticket, project_cfg) is not None:
            queue.enqueue(new_ticket)
            _log.info(
                "ticket re-enqueued for next stage",
                extra={
                    "project": new_ticket.project_name,
                    "issue": new_ticket.issue_number,
                    "labels": sorted(new_ticket.labels),
                },
            )
        else:
            _log.info(
                "ticket parked after role completion",
                extra={
                    "project": new_ticket.project_name,
                    "issue": new_ticket.issue_number,
                    "labels": sorted(new_ticket.labels),
                },
            )

    return True
