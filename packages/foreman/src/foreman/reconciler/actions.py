"""Action catalog for v3 — what the reconciler can do, and the context it
needs to do it. The executor itself lands in Task 7; this module first
establishes the enum + context shape so rules (Task 5+) can reference them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.state import IssueState, PRState, ProjectSnapshot


class Action(enum.Enum):
    """Catalog of every state-changing operation the reconciler can emit.

    Order matches the spec's transition flow for readability; the enum is
    NOT ordered (no value-comparison semantics intended).
    """

    NOOP = "noop"
    SURFACE_HELP = "surface_help"
    DISPATCH_PLANNER = "dispatch_planner"
    MERGE_SPEC_PR = "merge_spec_pr"
    ADVANCE_LABEL_TO_PLAN_APPROVED = "advance_label_to_plan_approved"
    DISPATCH_WORKER = "dispatch_worker"
    DISPATCH_REVIEWER = "dispatch_reviewer"
    DISPATCH_FIXER = "dispatch_fixer"
    MERGE_IMPL_PR = "merge_impl_pr"
    ADVANCE_LABEL_TO_DONE = "advance_label_to_done"


@dataclass(frozen=True)
class ActionContext:
    """Everything a rule + executor need to evaluate or apply an action.

    `snapshot` is the full project view. `issue` is the focal ticket.
    `pr` is the linked PR if one exists (None for tickets pre-PR or after merge).
    `log` is the execution log — rules consult it for idempotence; executor
    writes through it.
    """

    snapshot: ProjectSnapshot
    issue: IssueState
    pr: PRState | None
    log: ExecutionLog

    @property
    def ticket_id(self) -> str:
        return self.snapshot.ticket_id_for(self.issue.number)
