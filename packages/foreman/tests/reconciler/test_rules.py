"""Tests for the rule evaluator. Specific rule predicates land in Tasks 5+6;
this module covers the evaluator's behavior over the catalog."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from foreman.reconciler.actions import Action, ActionContext
from foreman.reconciler.exec_log import ExecutionLog
from foreman.reconciler.rules import PrecedenceTier, Rule, evaluate
from foreman.reconciler.state import IssueState, ProjectSnapshot, PRState


def _ctx(tmp_path: Path) -> ActionContext:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = ProjectSnapshot(
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
                updated_at=datetime(2026, 6, 3, tzinfo=UTC),
            ),
        ),
        prs=(),
        fetched_at=datetime(2026, 6, 3, tzinfo=UTC),
    )
    return ActionContext(snapshot=snap, issue=snap.issues[0], pr=None, log=log)


def test_evaluate_empty_catalog_returns_noop(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert evaluate(ctx, rules=()) is Action.NOOP


def test_evaluate_first_matching_rule_wins(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    catalog = (
        Rule(
            name="never_fires",
            tier=PrecedenceTier.SAFETY,
            precedence=10,
            when=lambda c: False,
            then=Action.SURFACE_HELP,
        ),
        Rule(
            name="always_fires",
            tier=PrecedenceTier.SAFETY,
            precedence=20,
            when=lambda c: True,
            then=Action.SURFACE_HELP,
        ),
        Rule(
            name="would_fire_if_reached",
            tier=PrecedenceTier.FORWARD_PROGRESS,
            precedence=100,
            when=lambda c: True,
            then=Action.DISPATCH_PLANNER,
        ),
    )
    assert evaluate(ctx, rules=catalog) is Action.SURFACE_HELP


def test_evaluate_no_match_returns_noop(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    catalog = (
        Rule(
            name="never_fires",
            tier=PrecedenceTier.SAFETY,
            precedence=10,
            when=lambda c: False,
            then=Action.SURFACE_HELP,
        ),
    )
    assert evaluate(ctx, rules=catalog) is Action.NOOP


def test_evaluate_predicate_exception_treated_as_no_match(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    catalog = (
        Rule(
            name="raiser",
            tier=PrecedenceTier.SAFETY,
            precedence=10,
            when=lambda c: (_ for _ in ()).throw(RuntimeError("boom")),
            then=Action.SURFACE_HELP,
        ),
        Rule(
            name="rescuer",
            tier=PrecedenceTier.FORWARD_PROGRESS,
            precedence=100,
            when=lambda c: True,
            then=Action.DISPATCH_PLANNER,
        ),
    )
    assert evaluate(ctx, rules=catalog) is Action.DISPATCH_PLANNER


# --- Safety rule cases ---


def _issue(labels: tuple[str, ...] = (), **overrides) -> IssueState:
    base = dict(
        number=143,
        title="t",
        labels=labels,
        assignees=(),
        body="",
        updated_at=datetime(2026, 6, 3, tzinfo=UTC),
    )
    base.update(overrides)
    return IssueState(**base)


def _pr(
    *,
    mergeable: str = "MERGEABLE",
    ci_status: str | None = "SUCCESS",
    is_merged: bool = False,
    linked: tuple[int, ...] = (143,),
) -> PRState:
    return PRState(
        number=144,
        head_ref="spec-143",
        mergeable=mergeable,
        ci_status=ci_status,
        body="Implements #143",
        linked_issue_numbers=linked,
        is_merged=is_merged,
    )


def _ctx_with(tmp_path: Path, issue: IssueState, pr=None) -> ActionContext:
    log = ExecutionLog(tmp_path / "log.sqlite")
    log.init()
    snap = ProjectSnapshot(
        project="foreman",
        owner="jeffrichley",
        repo="foreman",
        issues=(issue,),
        prs=(pr,) if pr else (),
        fetched_at=datetime(2026, 6, 3, tzinfo=UTC),
    )
    return ActionContext(snapshot=snap, issue=issue, pr=pr, log=log)


def test_needs_help_label_fires_surface_help(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:needs-help",)))
    assert evaluate(ctx, rules=RULES) is Action.SURFACE_HELP


def test_mergeable_conflict_fires_surface_help(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-review",)),
        _pr(mergeable="CONFLICTING"),
    )
    assert evaluate(ctx, rules=RULES) is Action.SURFACE_HELP


def test_ci_failure_on_impl_pr_fires_surface_help(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-review",)),
        _pr(ci_status="FAILURE"),
    )
    assert evaluate(ctx, rules=RULES) is Action.SURFACE_HELP


def test_surface_help_rate_limited_within_one_hour(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    issue = _issue(labels=("foreman:needs-help",))
    ctx = _ctx_with(tmp_path, issue)
    # Pre-seed an outcome=success surface_help row from "now".
    ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="needs_help_label",
        action="surface_help",
        outcome="success",
        details={},
    )
    # Within the rate-limit window: SHOULD NOT fire again (drops to NOOP because
    # the forward-progress catalog has nothing to do for a stuck planning ticket
    # with no PR).
    assert evaluate(ctx, rules=RULES) is Action.NOOP


def test_no_safety_condition_does_not_emit_surface_help(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:planning",)))
    # Forward-progress catalog might fire; but no safety condition means SURFACE_HELP
    # is not the answer.
    assert evaluate(ctx, rules=RULES) is not Action.SURFACE_HELP


# --- Forward-progress rule cases ---


def test_dispatch_planner_fires_on_planning_no_pr(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:planning",)))
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_PLANNER


def test_dispatch_planner_skipped_when_already_running(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:planning",)))
    ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )
    assert evaluate(ctx, rules=RULES) is Action.NOOP


def test_merge_spec_pr_fires_when_planning_pr_green(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    assert evaluate(ctx, rules=RULES) is Action.MERGE_SPEC_PR


def test_advance_label_to_plan_approved_when_spec_pr_merged(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(is_merged=True),
    )
    assert evaluate(ctx, rules=RULES) is Action.ADVANCE_LABEL_TO_PLAN_APPROVED


def test_advance_label_to_plan_approved_idempotent(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(is_merged=True),
    )
    # Pre-seed the advance as already done.
    ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="advance_label_to_plan_approved",
        action="advance_label_to_plan_approved",
        outcome="success",
        details={"from": "foreman:planning", "to": "foreman:plan-approved"},
    )
    assert evaluate(ctx, rules=RULES) is Action.NOOP


def test_dispatch_worker_fires_on_plan_approved(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:plan-approved",)))
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_WORKER


def test_dispatch_reviewer_fires_on_impl_review_green(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-review",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_REVIEWER


def test_dispatch_fixer_fires_on_impl_fix_label(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-fix",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_FIXER


def test_merge_impl_pr_fires_on_impl_approved(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    assert evaluate(ctx, rules=RULES) is Action.MERGE_IMPL_PR


def test_advance_label_to_done_when_impl_pr_merged(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(is_merged=True),
    )
    assert evaluate(ctx, rules=RULES) is Action.ADVANCE_LABEL_TO_DONE


def test_hold_label_blocks_all_actions(tmp_path: Path) -> None:
    """A ticket with foreman:hold should produce NOOP even when other rules would fire."""
    from foreman.reconciler.rules import RULES
    # Ticket has BOTH foreman:hold AND foreman:planning. Without hold, planning
    # would fire dispatch_planner. With hold, NOOP.
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:hold", "foreman:planning")))
    assert evaluate(ctx, rules=RULES) is Action.NOOP
