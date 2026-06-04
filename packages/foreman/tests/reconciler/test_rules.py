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
    head_ref: str = "foreman/issue-143",
) -> PRState:
    # Default head_ref is v3 spec-shaped (``foreman/issue-<N>``) so existing
    # spec-PR rule tests still match the head-ref filter added by adversarial
    # review fix 4c. Impl-PR tests must explicitly pass
    # ``head_ref="foreman/impl-<N>"``.
    return PRState(
        number=144,
        head_ref=head_ref,
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
        _pr(ci_status="FAILURE", head_ref="foreman/impl-143"),
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


def test_dispatch_planner_does_not_re_fire_after_spec_pr_closed_without_merge(
    tmp_path: Path,
) -> None:
    """If a human closes the spec PR without merging, ``ctx.pr`` flips back
    to None while the issue still carries ``foreman:planning``.  Without a
    ``count_completed == 0`` gate, the rule would re-fire dispatch_planner
    and spawn a second Planner subprocess that opens another spec PR
    (adversarial review).
    """
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:planning",)))
    # Simulate a prior Planner run that has completed (success outcome).
    start_id = ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )
    ctx.log.terminate_action(parent_log_id=start_id, outcome="success", details={})
    # PR is now None (closed without merge) but label is still planning.
    # Rule must NOT re-fire.
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


def test_dispatch_reviewer_spec_re_fires_after_prior_completed_run(
    tmp_path: Path,
) -> None:
    """After a prior Reviewer-spec run completes and a Fixer-spec cycle
    transitions spec-fix → planning, the Reviewer must re-fire on the
    updated spec PR — even though one dispatch_reviewer_spec row has
    already terminated for this ticket (adversarial review HIGH #6).

    Pre-fix, the rule had a ``count_completed("dispatch_reviewer_spec") == 0``
    gate that flipped permanently False after the first Reviewer run, dead-
    locking every spec-fix → planning re-review. The fix drops that gate;
    the label-state machine + has_unterminated check are the right gates.
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
        outcome="success",
        details={},
    )
    ctx.log.terminate_action(parent_log_id=start_id, outcome="success", details={})
    # Label is back on planning (simulating Fixer-spec's spec-fix→planning
    # transition); the rule must re-fire.
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_REVIEWER_SPEC


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
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143"),
    )
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_REVIEWER_IMPL


def test_dispatch_fixer_impl_fires_on_impl_fix_label(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-fix",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143"),
    )
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_FIXER_IMPL


def test_merge_impl_pr_fires_on_impl_approved(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143"),
        auto_merge_impl=True,
    )
    assert evaluate(ctx, rules=RULES) is Action.MERGE_IMPL_PR


def test_merge_impl_pr_requires_flag(tmp_path: Path) -> None:
    """auto_merge_impl=False parks the PR at impl-approved (no auto-merge)."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143"),
        auto_merge_impl=False,
    )
    assert evaluate(ctx, rules=RULES) is not Action.MERGE_IMPL_PR


def test_merge_impl_pr_fires_when_flag_on(tmp_path: Path) -> None:
    """merge_impl_pr fires on impl-approved + green + flag on."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143"),
        auto_merge_impl=True,
    )
    assert evaluate(ctx, rules=RULES) is Action.MERGE_IMPL_PR


def test_advance_label_to_done_when_impl_pr_merged(tmp_path: Path) -> None:
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(is_merged=True, head_ref="foreman/impl-143"),
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
    pr = _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143")
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
    pr = _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143")
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


def test_spec_reviewer_does_not_refire_when_label_off_planning(tmp_path: Path) -> None:
    """The spec-side rule is gated by ``foreman:planning`` in labels. After
    a Reviewer run succeeds and the Reviewer's label transition has moved
    the label off planning (e.g., to spec-fix or plan-approved), the rule
    must not re-fire — even though a prior dispatch_reviewer_spec row exists.

    This is the right idempotence post-HIGH #6 fix: the label state, not a
    count_completed gate, is what stops re-fire. Combined with the
    has_unterminated check (covered separately), it produces the desired
    spec-fix→planning re-review without deadlock.
    """
    from foreman.reconciler.rules import RULES

    # Label is foreman:spec-fix (post-Reviewer-needs_fix), NOT planning.
    issue = _issue(labels=("foreman:spec-fix",))
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

    # Label is off planning; spec-side Reviewer rule must NOT fire.
    assert evaluate(ctx, rules=RULES) is not Action.DISPATCH_REVIEWER_SPEC


# --- CRITICAL #3: dispatch_fixer_spec (spec-side fix loop) ---


def test_dispatch_fixer_spec_fires_on_spec_fix_label(tmp_path: Path) -> None:
    """Spec-fix label + open spec PR + no Fixer in-flight → dispatch Fixer (spec)."""
    from foreman.reconciler.rules import RULES

    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:spec-fix",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_FIXER_SPEC


def test_dispatch_fixer_spec_blocked_when_in_flight(tmp_path: Path) -> None:
    """An unterminated dispatch_fixer_spec row prevents re-fire."""
    from foreman.reconciler.rules import RULES

    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:spec-fix",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="dispatch_fixer_spec",
        action="dispatch_fixer_spec",
        outcome="running",
        details={},
    )
    assert evaluate(ctx, rules=RULES) is not Action.DISPATCH_FIXER_SPEC


def test_dispatch_fixer_spec_attempts_exhausted_surfaces_help(tmp_path: Path) -> None:
    """After _MAX_FIX_ATTEMPTS completed Fixer-spec runs, surface_help fires."""
    from foreman.reconciler.rules import RULES

    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:spec-fix",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    for _ in range(3):  # _MAX_FIX_ATTEMPTS = 3
        start_id = ctx.log.write_action(
            ticket_id=ctx.ticket_id,
            project="foreman",
            rule_name="dispatch_fixer_spec",
            action="dispatch_fixer_spec",
            outcome="running",
            details={},
        )
        ctx.log.terminate_action(parent_log_id=start_id, outcome="success", details={})
    assert evaluate(ctx, rules=RULES) is Action.SURFACE_HELP


# --- MEDIUM #4c: safety rules filter by PR head_ref, not label alone ---


def test_spec_pr_ci_failure_ignores_impl_shaped_pr(tmp_path: Path) -> None:
    """A FAILURE CI on an impl-shaped PR must NOT trigger the spec-side
    safety rule even if the issue still carries foreman:planning.

    During the brief stacked window where both spec and impl PRs are
    linked to the same issue, a label-only filter could fire the wrong
    safety rule on the wrong PR. The head-ref filter prevents that.
    """
    from foreman.reconciler.rules import RULES

    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(
            mergeable="MERGEABLE",
            ci_status="FAILURE",
            head_ref="foreman/impl-143",
        ),
    )
    # No spec-side safety rule should fire — head_ref is impl-shaped. The
    # impl_pr_ci_failure rule also won't fire because the labels don't include
    # an impl-side label. End result: NOOP / no SURFACE_HELP.
    assert evaluate(ctx, rules=RULES) is not Action.SURFACE_HELP


def test_impl_pr_ci_failure_ignores_spec_shaped_pr(tmp_path: Path) -> None:
    """A FAILURE CI on a spec-shaped PR must NOT trigger the impl-side
    safety rule even if the issue still carries an impl-side label."""
    from foreman.reconciler.rules import RULES

    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-review",)),
        _pr(
            mergeable="MERGEABLE",
            ci_status="FAILURE",
            head_ref="foreman/issue-143",
        ),
    )
    # impl-side safety rule (impl_pr_ci_failure) must refuse to match because
    # head_ref isn't impl-shaped. spec-side safety rule also won't match
    # (no foreman:planning label). End result: no SURFACE_HELP.
    assert evaluate(ctx, rules=RULES) is not Action.SURFACE_HELP


# --- MEDIUM #11: merge_*_pr + lagging-label rules filter by head_ref ---


def test_merge_spec_pr_does_not_fire_against_impl_pr(tmp_path: Path) -> None:
    """Defense in depth: even if the daemon's picker handed an impl PR into
    ``ctx.pr`` for a plan-approved issue (transient stacked-PR window or
    legacy state), ``merge_spec_pr`` must refuse to fire because the PR's
    head_ref isn't spec-shaped. Otherwise the executor would call
    ``host.merge_pr(pr_number=<impl_pr>)`` while logging the rule name as
    "merge_spec_pr"."""
    from foreman.reconciler.rules import RULES

    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:plan-approved",)),
        _pr(
            mergeable="MERGEABLE",
            ci_status="SUCCESS",
            head_ref="foreman/impl-143",
        ),
        auto_merge_spec=True,
    )
    assert evaluate(ctx, rules=RULES) is not Action.MERGE_SPEC_PR


def test_merge_impl_pr_does_not_fire_against_spec_pr(tmp_path: Path) -> None:
    """Symmetric to ``test_merge_spec_pr_does_not_fire_against_impl_pr``:
    even with ``foreman:impl-approved`` + green CI + auto_merge_impl=True,
    a spec-shaped PR must not be merged by ``merge_impl_pr``."""
    from foreman.reconciler.rules import RULES

    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(
            mergeable="MERGEABLE",
            ci_status="SUCCESS",
            head_ref="foreman/issue-143",
        ),
        auto_merge_impl=True,
    )
    assert evaluate(ctx, rules=RULES) is not Action.MERGE_IMPL_PR


def test_advance_label_to_plan_approved_ignores_impl_shaped_pr(
    tmp_path: Path,
) -> None:
    """A merged impl-shaped PR sitting on a planning-labeled ticket
    (very transient) must NOT drive ``advance_label_to_plan_approved`` —
    only a spec merge should transition planning → plan-approved."""
    from foreman.reconciler.rules import RULES

    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(is_merged=True, head_ref="foreman/impl-143"),
    )
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_PLAN_APPROVED


def test_advance_label_to_done_ignores_spec_shaped_pr(tmp_path: Path) -> None:
    """A merged spec-shaped PR sitting on an impl-approved-labeled ticket
    (transient) must NOT drive ``advance_label_to_done`` — only an impl
    merge should transition impl-approved → done."""
    from foreman.reconciler.rules import RULES

    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(is_merged=True, head_ref="foreman/issue-143"),
    )
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_DONE
