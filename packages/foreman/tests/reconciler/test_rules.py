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
    review_decision: str | None = None,
) -> PRState:
    return PRState(
        number=144,
        head_ref="spec-143",
        mergeable=mergeable,
        ci_status=ci_status,
        body="Implements #143",
        linked_issue_numbers=linked,
        is_merged=is_merged,
        review_decision=review_decision,
    )


def _ctx_with(
    tmp_path: Path,
    issue: IssueState,
    pr=None,
    *,
    auto_merge_spec: bool = True,
    auto_merge_impl: bool = False,
) -> ActionContext:
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
    return ActionContext(
        snapshot=snap,
        issue=issue,
        pr=pr,
        log=log,
        auto_merge_spec=auto_merge_spec,
        auto_merge_impl=auto_merge_impl,
    )


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


def test_dispatch_reviewer_spec_fires_when_planning_pr_open_no_review_yet(
    tmp_path: Path,
) -> None:
    """Spec PR sitting open with no Reviewer dispatch yet → dispatch_reviewer_spec."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_REVIEWER_SPEC


def test_dispatch_reviewer_spec_skipped_when_reviewer_in_flight(tmp_path: Path) -> None:
    """If a spec-side Reviewer dispatch is unterminated, don't re-fire."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="dispatch_reviewer_spec",
        action="dispatch_reviewer_spec",
        outcome="running",
        details={},
    )
    # No other rule should match this configuration either → NOOP.
    assert evaluate(ctx, rules=RULES) is Action.NOOP


def test_dispatch_reviewer_spec_does_not_refire_after_reviewer_completed(
    tmp_path: Path,
) -> None:
    """Once a dispatch_reviewer_spec log row terminates for this ticket, the
    rule should not fire again — even if the planning label is still on (the
    label transition is the Reviewer subprocess's responsibility, not the rule).
    """
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    # Seed a completed dispatch_reviewer_spec record: start running, then terminate.
    start_id = ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="dispatch_reviewer_spec",
        action="dispatch_reviewer_spec",
        outcome="running",
        details={},
    )
    ctx.log.terminate_action(parent_log_id=start_id, outcome="success", details={})
    assert evaluate(ctx, rules=RULES) is not Action.DISPATCH_REVIEWER_SPEC


def test_merge_spec_pr_now_requires_plan_approved_label_and_flag(
    tmp_path: Path,
) -> None:
    """merge_spec_pr only fires on plan-approved label + green + flag on."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:plan-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
        auto_merge_spec=True,
    )
    assert evaluate(ctx, rules=RULES) is Action.MERGE_SPEC_PR


def test_merge_spec_pr_blocked_when_flag_off(tmp_path: Path) -> None:
    """auto_merge_spec=False parks the PR at plan-approved (no auto-merge)."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:plan-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
        auto_merge_spec=False,
    )
    assert evaluate(ctx, rules=RULES) is not Action.MERGE_SPEC_PR


def test_merge_spec_pr_no_longer_fires_on_planning_label(tmp_path: Path) -> None:
    """The old behavior — fire on foreman:planning with green CI — is GONE."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
        auto_merge_spec=True,
    )
    assert evaluate(ctx, rules=RULES) is not Action.MERGE_SPEC_PR


def test_advance_label_to_plan_approved_when_spec_pr_merged(tmp_path: Path) -> None:
    """Safety-net lagging rule still fires when PR merged + label hasn't swapped."""
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
        rule_name="advance_label_to_plan_approved_lagging",
        action="advance_label_to_plan_approved",
        outcome="success",
        details={"from": "foreman:planning", "to": "foreman:plan-approved"},
    )
    assert evaluate(ctx, rules=RULES) is Action.NOOP


def test_dispatch_worker_fires_on_plan_approved(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:plan-approved",)))
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_WORKER


def test_dispatch_reviewer_impl_fires_on_impl_review_green(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-review",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_REVIEWER_IMPL


def test_dispatch_fixer_impl_fires_on_impl_fix_label(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-fix",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_FIXER_IMPL


def test_merge_impl_pr_fires_on_impl_approved(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
        auto_merge_impl=True,
    )
    assert evaluate(ctx, rules=RULES) is Action.MERGE_IMPL_PR


def test_merge_impl_pr_requires_flag(tmp_path: Path) -> None:
    """auto_merge_impl=False parks the PR at impl-approved (no auto-merge)."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
        auto_merge_impl=False,
    )
    assert evaluate(ctx, rules=RULES) is not Action.MERGE_IMPL_PR


def test_merge_impl_pr_fires_when_flag_on(tmp_path: Path) -> None:
    """merge_impl_pr fires on impl-approved + green + flag on."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
        auto_merge_impl=True,
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


def test_dispatch_fixer_blocked_after_3_completed_attempts(tmp_path: Path) -> None:
    """Once 3 dispatch_fixer_impl attempts have completed, the budget is
    exhausted and the budget-exhausted safety rule fires (SURFACE_HELP),
    preempting any further dispatch_fixer_impl."""
    from foreman.reconciler.rules import RULES

    issue = _issue(labels=("foreman:impl-fix",))
    pr = _pr(mergeable="MERGEABLE", ci_status="SUCCESS")
    ctx = _ctx_with(tmp_path, issue, pr)
    for _ in range(3):
        start_id = ctx.log.write_action(
            ticket_id=ctx.ticket_id, project="foreman", rule_name="dispatch_fixer_impl",
            action="dispatch_fixer_impl", outcome="running", details={},
        )
        ctx.log.terminate_action(parent_log_id=start_id, outcome="error", details={})

    assert evaluate(ctx, rules=RULES) is Action.SURFACE_HELP


def test_dispatch_fixer_still_fires_under_budget(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES

    issue = _issue(labels=("foreman:impl-fix",))
    pr = _pr(mergeable="MERGEABLE", ci_status="SUCCESS")
    ctx = _ctx_with(tmp_path, issue, pr)
    # 2 completed attempts — under cap of 3
    for _ in range(2):
        start_id = ctx.log.write_action(
            ticket_id=ctx.ticket_id, project="foreman", rule_name="dispatch_fixer_impl",
            action="dispatch_fixer_impl", outcome="running", details={},
        )
        ctx.log.terminate_action(parent_log_id=start_id, outcome="success", details={})

    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_FIXER_IMPL


def test_dispatch_worker_blocked_after_3_completed_attempts(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES

    issue = _issue(labels=("foreman:plan-approved",))
    ctx = _ctx_with(tmp_path, issue)
    for _ in range(3):
        start_id = ctx.log.write_action(
            ticket_id=ctx.ticket_id, project="foreman", rule_name="dispatch_worker",
            action="dispatch_worker", outcome="running", details={},
        )
        ctx.log.terminate_action(parent_log_id=start_id, outcome="error", details={})

    assert evaluate(ctx, rules=RULES) is Action.SURFACE_HELP


# --- HIGH #7 regression: spec-vs-impl Reviewer count_completed isolation ---


def test_spec_and_impl_reviewer_count_completed_are_independent(tmp_path: Path) -> None:
    """A completed impl-PR Reviewer dispatch must NOT block a future spec-PR
    Reviewer dispatch on the same ticket (e.g., after a manual reopen / a
    second planning cycle).

    Before the action split: both spec-side ``_planning_pr_needs_review`` and
    the impl-side dispatch_reviewer rule shared the action key
    ``dispatch_reviewer``. Once an impl-side dispatch completed, the spec-side
    gate's ``count_completed(...) == 0`` predicate flipped permanently False,
    silently dropping the spec re-review. Splitting the actions by target
    fixes this — the spec-side gate now counts only ``dispatch_reviewer_spec``.
    """
    from foreman.reconciler.rules import RULES

    issue = _issue(labels=("foreman:planning",))
    pr = _pr(mergeable="MERGEABLE", ci_status="SUCCESS")
    ctx = _ctx_with(tmp_path, issue, pr)

    # Pre-seed a completed impl-side Reviewer dispatch for this ticket.
    start_id = ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="dispatch_reviewer_impl",
        action="dispatch_reviewer_impl",
        outcome="running",
        details={},
    )
    ctx.log.terminate_action(parent_log_id=start_id, outcome="success", details={})

    # Spec-side rule must still fire — its count_completed("dispatch_reviewer_spec", ...) is 0.
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_REVIEWER_SPEC


def test_spec_reviewer_completed_blocks_further_spec_dispatch(tmp_path: Path) -> None:
    """The post-split idempotence on the spec side still works: a completed
    ``dispatch_reviewer_spec`` row stops the spec-side rule from re-firing."""
    from foreman.reconciler.rules import RULES

    issue = _issue(labels=("foreman:planning",))
    pr = _pr(mergeable="MERGEABLE", ci_status="SUCCESS")
    ctx = _ctx_with(tmp_path, issue, pr)

    start_id = ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="dispatch_reviewer_spec",
        action="dispatch_reviewer_spec",
        outcome="running",
        details={},
    )
    ctx.log.terminate_action(parent_log_id=start_id, outcome="success", details={})

    # Once the spec-side reviewer has run, the spec-side rule must NOT re-fire.
    assert evaluate(ctx, rules=RULES) is not Action.DISPATCH_REVIEWER_SPEC
