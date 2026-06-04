"""Action catalog for v3 — what the reconciler can do, and the context it
needs to do it. The executor itself lands in Task 7; this module first
establishes the enum + context shape so rules (Task 5+) can reference them.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass

from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.host import ReconcilerHost
from foreman.reconciler.state import IssueState, ProjectSnapshot, PRState

logger = logging.getLogger(__name__)


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
    # foreman v3 rescue Stage 2: split Reviewer + Fixer dispatch actions
    # by target. Pre-split, both spec-side and impl-side dispatch_reviewer
    # rules shared the action key ``dispatch_reviewer``, which made the
    # spec-side ``count_completed`` idempotence gate flip permanently
    # False once an impl-side dispatch landed (HIGH #7). Same shape for
    # the Fixer: ``foreman fix --target spec_pr|impl_pr`` requires per-
    # target action so the executor can plumb the right flag (CRITICAL #4).
    DISPATCH_REVIEWER_SPEC = "dispatch_reviewer_spec"
    DISPATCH_REVIEWER_IMPL = "dispatch_reviewer_impl"
    DISPATCH_FIXER_SPEC = "dispatch_fixer_spec"
    DISPATCH_FIXER_IMPL = "dispatch_fixer_impl"
    MERGE_IMPL_PR = "merge_impl_pr"
    ADVANCE_LABEL_TO_DONE = "advance_label_to_done"


@dataclass(frozen=True)
class ActionContext:
    """Everything a rule + executor need to evaluate or apply an action.

    `snapshot` is the full project view. `issue` is the focal ticket.
    `pr` is the linked PR if one exists (None for tickets pre-PR or after merge).
    `log` is the execution log — rules consult it for idempotence; executor
    writes through it.

    `auto_merge_spec` and `auto_merge_impl` are the EFFECTIVE per-project
    auto-merge flags (after global+project resolution via
    ``ReconcilerConfig.effective_auto_merge_*``). Rules consult these to
    decide between MERGE_SPEC_PR / MERGE_IMPL_PR and a "park for human
    review" transition. Defaults match the global ``ReconcilerConfig``
    defaults (spec=True, impl=False) so legacy callers that omit the flags
    keep the same behavior.
    """

    snapshot: ProjectSnapshot
    issue: IssueState
    pr: PRState | None
    log: ExecutionLog
    auto_merge_spec: bool = True
    auto_merge_impl: bool = False

    @property
    def ticket_id(self) -> str:
        return self.snapshot.ticket_id_for(self.issue.number)


_DISPATCH_ROLE_FOR_ACTION: dict[Action, tuple[str, str | None]] = {
    Action.DISPATCH_PLANNER: ("planner", None),
    Action.DISPATCH_WORKER: ("worker", None),
    Action.DISPATCH_REVIEWER_SPEC: ("reviewer", "spec_pr"),
    Action.DISPATCH_REVIEWER_IMPL: ("reviewer", "impl_pr"),
    Action.DISPATCH_FIXER_SPEC: ("fixer", "spec_pr"),
    Action.DISPATCH_FIXER_IMPL: ("fixer", "impl_pr"),
}


def execute_action(
    action: Action,
    ctx: ActionContext,
    *,
    host: ReconcilerHost,
    rule_name: str,
    dry_run: bool,
) -> None:
    """Execute one action with single-writer log + dry-run support.

    Sequence: write start row -> call host -> write termination row (success
    or error). On exception the start row is terminated with outcome='error'
    and the exception is logged; the executor never re-raises (one bad action
    must not crash the reconciler loop).

    For dry_run: skip host entirely, write a single row with outcome='dry_run'.
    """
    if action is Action.NOOP:
        return

    if dry_run:
        ctx.log.write_action(
            ticket_id=ctx.ticket_id,
            project=ctx.snapshot.project,
            rule_name=rule_name,
            action=action.value,
            outcome="dry_run",
            details={
                "issue": ctx.issue.number,
                "pr": ctx.pr.number if ctx.pr else None,
            },
        )
        return

    start_id = ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project=ctx.snapshot.project,
        rule_name=rule_name,
        action=action.value,
        outcome="running",
        details={
            "issue": ctx.issue.number,
            "pr": ctx.pr.number if ctx.pr else None,
        },
    )

    try:
        if action is Action.SURFACE_HELP:
            host.add_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:needs-help",
            )
            host.post_comment(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                body=(
                    "Foreman v3 surfaced this ticket for human attention. "
                    "Investigate state and either fix or remove the "
                    "`foreman:needs-help` label to resume autonomous flow."
                ),
            )
        elif action in _DISPATCH_ROLE_FOR_ACTION:
            role, target = _DISPATCH_ROLE_FOR_ACTION[action]
            host.dispatch_role(
                role=role,
                target=target,
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                pr_number=ctx.pr.number if ctx.pr else None,
                start_log_id=start_id,
            )
        elif action is Action.MERGE_SPEC_PR or action is Action.MERGE_IMPL_PR:
            if ctx.pr is None:
                raise RuntimeError(f"{action.name} requires a PR in context")
            host.merge_pr(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                pr_number=ctx.pr.number,
            )
        elif action is Action.ADVANCE_LABEL_TO_PLAN_APPROVED:
            host.remove_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:planning",
            )
            host.add_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:plan-approved",
            )
        elif action is Action.ADVANCE_LABEL_TO_DONE:
            host.remove_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:impl-approved",
            )
            host.add_label(
                owner=ctx.snapshot.owner,
                repo=ctx.snapshot.repo,
                issue=ctx.issue.number,
                label="foreman:done",
            )

        # Some actions complete synchronously (label changes, merges,
        # surface_help) and we write the success termination here. Subprocess
        # dispatches return from this branch already — the host's background
        # tracker (or terminate_dispatch in synchronous tests) writes the
        # termination row when the subprocess exits, so we must NOT write a
        # success row here or count_completed would double-count.
        if action in _DISPATCH_ROLE_FOR_ACTION:
            return

        ctx.log.terminate_action(parent_log_id=start_id, outcome="success", details={})

    except Exception as exc:
        logger.exception("action %s failed for ticket %s", action.name, ctx.ticket_id)
        ctx.log.terminate_action(
            parent_log_id=start_id,
            outcome="error",
            details={"error": str(exc)},
        )
