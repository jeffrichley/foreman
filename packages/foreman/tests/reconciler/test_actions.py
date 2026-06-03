"""Tests for v3 actions — enum, context, executor."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from foreman.reconciler.actions import Action, ActionContext
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.state import IssueState, PRState, ProjectSnapshot


def _snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(
            IssueState(
                number=143,
                title="t",
                labels=("foreman:planning",),
                assignees=(),
                body="",
                updated_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
            ),
        ),
        prs=(),
        fetched_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )


def test_action_enum_covers_spec_catalog() -> None:
    expected = {
        "NOOP",
        "SURFACE_HELP",
        "DISPATCH_PLANNER",
        "MERGE_SPEC_PR",
        "ADVANCE_LABEL_TO_PLAN_APPROVED",
        "DISPATCH_WORKER",
        "DISPATCH_REVIEWER",
        "DISPATCH_FIXER",
        "MERGE_IMPL_PR",
        "ADVANCE_LABEL_TO_DONE",
    }
    assert {a.name for a in Action} == expected


def test_action_context_exposes_ticket_id(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    issue = snap.issues[0]
    ctx = ActionContext(snapshot=snap, issue=issue, pr=None, log=log)
    assert ctx.ticket_id == "jeffrichley/foreman#143"


def test_action_context_carries_pr_when_present(tmp_path: Path) -> None:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = _snapshot()
    issue = snap.issues[0]
    pr = PRState(
        number=144,
        head_ref="spec-143",
        mergeable="MERGEABLE",
        ci_status="SUCCESS",
        body="Implements #143",
        linked_issue_numbers=(143,),
        is_merged=False,
    )
    ctx = ActionContext(snapshot=snap, issue=issue, pr=pr, log=log)
    assert ctx.pr is pr
