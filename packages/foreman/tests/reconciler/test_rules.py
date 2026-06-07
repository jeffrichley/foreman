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


def test_advance_label_to_planning_fires_on_plan_only_ticket(tmp_path: Path) -> None:
    """foreman#171: a fresh ticket labeled only ``foreman:plan`` should
    auto-transition to ``foreman:planning`` so ``dispatch_planner`` fires
    on the next poll."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:plan",)))
    assert evaluate(ctx, rules=RULES) is Action.ADVANCE_LABEL_TO_PLANNING


def test_advance_label_to_planning_suppressed_by_planning_label(tmp_path: Path) -> None:
    """A ticket with both ``foreman:plan`` and ``foreman:planning`` is past
    the queue phase; the new rule must not fire (no double-transition)."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path, _issue(labels=("foreman:plan", "foreman:planning"))
    )
    # dispatch_planner at prec 100 wins over our prec-95 rule when its
    # predicate matches, but the test is really about: our rule's
    # predicate must NOT match here. Test directly via evaluate result:
    # whatever fires, it shouldn't be ADVANCE_LABEL_TO_PLANNING.
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_PLANNING


def test_advance_label_to_planning_suppressed_by_plan_approved(tmp_path: Path) -> None:
    """A stale ``foreman:plan`` alongside ``foreman:plan-approved`` must
    NOT re-advance — the ticket has already been through the Reviewer."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path, _issue(labels=("foreman:plan", "foreman:plan-approved"))
    )
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_PLANNING


def test_advance_label_to_planning_suppressed_by_hold(tmp_path: Path) -> None:
    """``foreman:hold`` preempts everything (belt-and-suspenders beyond the
    safety-tier rules) — a ``plan + hold`` ticket must not auto-advance."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path, _issue(labels=("foreman:plan", "foreman:hold"))
    )
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_PLANNING


def test_advance_label_to_planning_suppressed_by_needs_help(tmp_path: Path) -> None:
    """``foreman:needs-help`` means a human owns this — don't auto-advance
    even if ``foreman:plan`` is also present."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path, _issue(labels=("foreman:plan", "foreman:needs-help"))
    )
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_PLANNING


def test_advance_label_to_planning_suppressed_by_impl_attempt_counter(tmp_path: Path) -> None:
    """An ``foreman:impl-attempt-N`` label means the ticket has been
    through a Worker cycle; the daemon DB wipe scenario (2026-06-06)
    makes this the canonical signal since ``count_completed`` resets but
    the GH labels persist. Refuse to re-advance."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path, _issue(labels=("foreman:plan", "foreman:impl-attempt-1"))
    )
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_PLANNING


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


def test_dispatch_planner_re_fires_after_prior_error_termination(
    tmp_path: Path,
) -> None:
    """A prior Planner run that terminated with ``outcome='error'`` (e.g.,
    semaphore raise, subprocess crash, executor exception) did NOT open a
    spec PR. A subsequent tick MUST re-fire dispatch_planner — the failure
    must not count toward the idempotence gate.

    Pre-fix, ``_planning_no_pr`` counted ALL terminated rows, so any error
    (or ``errored:recovery`` from a daemon-restart sweep, or ``timeout``
    from the host's tracker) permanently blocked re-fire and silently
    dead-locked the ticket with no escalation rule.
    """
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:planning",)))
    # Seed a prior Planner attempt that errored mid-run (no spec PR opened).
    start_id = ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )
    ctx.log.terminate_action(parent_log_id=start_id, outcome="error", details={})
    # Error does NOT count toward the success-based idempotence gate; rule
    # must re-fire.
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_PLANNER


def test_dispatch_planner_re_fires_after_errored_recovery(tmp_path: Path) -> None:
    """A Planner run swept by ``recover_orphaned`` on daemon restart is
    terminated with ``outcome='errored:recovery'``. That counts as a crash,
    not a successful Planner run — re-fire must be allowed."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(tmp_path, _issue(labels=("foreman:planning",)))
    start_id = ctx.log.write_action(
        ticket_id=ctx.ticket_id,
        project="foreman",
        rule_name="dispatch_planner",
        action="dispatch_planner",
        outcome="running",
        details={},
    )
    ctx.log.terminate_action(
        parent_log_id=start_id, outcome="errored:recovery", details={}
    )
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_PLANNER


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


def test_advance_label_to_merging_plan_requires_plan_approved_label_and_flag(
    tmp_path: Path,
) -> None:
    """foreman#165 converted from ``test_merge_spec_pr_now_requires_...``:
    the new ``advance_label_to_merging_plan`` rule fires on the same
    predicate inputs (plan-approved label + auto_merge_spec=True) — but
    the action it emits is now the label-advance rather than a direct
    ``host.merge_pr`` call."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:plan-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
        auto_merge_spec=True,
    )
    assert evaluate(ctx, rules=RULES) is Action.ADVANCE_LABEL_TO_MERGING_PLAN


def test_advance_label_to_merging_plan_blocked_when_flag_off(tmp_path: Path) -> None:
    """foreman#165 converted from ``test_merge_spec_pr_blocked_when_flag_off``:
    auto_merge_spec=False parks the PR at plan-approved (no auto-merge);
    the new label-advance rule must not fire."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:plan-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
        auto_merge_spec=False,
    )
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_MERGING_PLAN


def test_advance_label_to_merging_plan_does_not_fire_on_planning_label(
    tmp_path: Path,
) -> None:
    """foreman#165 converted from
    ``test_merge_spec_pr_no_longer_fires_on_planning_label``: the new
    label-advance only fires on ``plan-approved``, not on ``planning`` —
    the same gating semantic as the removed merge_spec_pr rule."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
        auto_merge_spec=True,
    )
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_MERGING_PLAN


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


def test_advance_label_to_merging_impl_fires_on_impl_approved(tmp_path: Path) -> None:
    """foreman#165 converted from ``test_merge_impl_pr_fires_on_impl_approved``:
    impl-approved label + impl-shaped PR + auto_merge_impl=True → the new
    label-advance rule fires. The old direct merge_impl_pr action is gone."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143"),
        auto_merge_impl=True,
    )
    assert evaluate(ctx, rules=RULES) is Action.ADVANCE_LABEL_TO_MERGING_IMPL


def test_advance_label_to_merging_impl_requires_flag(tmp_path: Path) -> None:
    """foreman#165 converted from ``test_merge_impl_pr_requires_flag``:
    auto_merge_impl=False parks the PR at impl-approved; the label-advance
    must not fire."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143"),
        auto_merge_impl=False,
    )
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_MERGING_IMPL


# (Note: ``test_merge_impl_pr_fires_when_flag_on`` from the v2 catalog was
# a semantic duplicate of ``test_merge_impl_pr_fires_on_impl_approved``
# once both were converted to the new label-advance assertion. Deleted
# per the spec's disposition list — the single converted
# ``test_advance_label_to_merging_impl_fires_on_impl_approved`` above
# covers the case once.)


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


def test_advance_label_to_merging_plan_does_not_fire_against_impl_pr(
    tmp_path: Path,
) -> None:
    """foreman#165 converted from ``test_merge_spec_pr_does_not_fire_against_impl_pr``:
    defense in depth — even if the daemon's picker handed an impl PR into
    ``ctx.pr`` for a plan-approved issue (transient stacked-PR window or
    legacy state), ``advance_label_to_merging_plan`` must refuse to fire
    because the PR's head_ref isn't spec-shaped. Otherwise the wrong
    label would be added and the next tick's attempt_merge_plan would
    operate on the impl PR."""
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
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_MERGING_PLAN


def test_advance_label_to_merging_impl_does_not_fire_against_spec_pr(
    tmp_path: Path,
) -> None:
    """foreman#165 converted from ``test_merge_impl_pr_does_not_fire_against_spec_pr``:
    symmetric head-ref filter coverage — even with ``foreman:impl-approved``
    + auto_merge_impl=True, a spec-shaped PR must not be advanced into
    the merging-impl state by ``advance_label_to_merging_impl``."""
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
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_MERGING_IMPL


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


# --- Pass 3 HIGH: Reviewer attempt budget (spec + impl) ---


def test_dispatch_reviewer_spec_blocked_after_3_completed_attempts(tmp_path: Path) -> None:
    """After 3 completed Reviewer-spec runs (any outcome), the forward-progress
    rule must not fire; the safety budget-exhausted rule fires SURFACE_HELP.

    Before this fix, Plan B Stage 1+2 closed the silent-stall gap by
    terminating crashed Reviewer rows — but with no budget cap, a persistent
    root cause (e.g., LLM provider down) creates an infinite hot-spawn loop:
    crash → terminate → next tick re-fire → crash → ...
    Every other dispatch role has an attempt budget; this closes that gap.
    """
    from foreman.reconciler.rules import RULES

    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    for _ in range(3):
        start_id = ctx.log.write_action(
            ticket_id=ctx.ticket_id,
            project="foreman",
            rule_name="dispatch_reviewer_spec",
            action="dispatch_reviewer_spec",
            outcome="running",
            details={},
        )
        ctx.log.terminate_action(parent_log_id=start_id, outcome="error", details={})
    assert evaluate(ctx, rules=RULES) is Action.SURFACE_HELP


def test_dispatch_reviewer_spec_still_fires_under_budget(tmp_path: Path) -> None:
    """2 completed Reviewer-spec attempts is under the cap of 3 — the rule
    must still fire on a tick with no in-flight dispatch."""
    from foreman.reconciler.rules import RULES

    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:planning",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
    )
    for _ in range(2):
        start_id = ctx.log.write_action(
            ticket_id=ctx.ticket_id,
            project="foreman",
            rule_name="dispatch_reviewer_spec",
            action="dispatch_reviewer_spec",
            outcome="running",
            details={},
        )
        ctx.log.terminate_action(parent_log_id=start_id, outcome="error", details={})
    assert evaluate(ctx, rules=RULES) is Action.DISPATCH_REVIEWER_SPEC


def test_dispatch_reviewer_impl_blocked_after_3_completed_attempts(tmp_path: Path) -> None:
    """Symmetric to spec-side: after 3 completed Reviewer-impl runs, the
    forward-progress rule must not fire; the safety budget-exhausted rule
    fires SURFACE_HELP."""
    from foreman.reconciler.rules import RULES

    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-review",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143"),
    )
    for _ in range(3):
        start_id = ctx.log.write_action(
            ticket_id=ctx.ticket_id,
            project="foreman",
            rule_name="dispatch_reviewer_impl",
            action="dispatch_reviewer_impl",
            outcome="running",
            details={},
        )
        ctx.log.terminate_action(parent_log_id=start_id, outcome="error", details={})
    assert evaluate(ctx, rules=RULES) is Action.SURFACE_HELP


# ---------------------------------------------------------------------------
# foreman#165 — new rule predicates (advance_label_to_merging_* and
# attempt_merge_* per target)
# ---------------------------------------------------------------------------


def test_advance_label_to_merging_plan_fires_on_plan_approved_with_open_spec_pr_and_auto_merge_spec(
    tmp_path: Path,
) -> None:
    """The one-shot label transition fires when plan-approved + open
    spec PR + auto_merge_spec=True, AND no merging-* label is yet
    present. This is the new spec-side merge entry point."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:plan-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
        auto_merge_spec=True,
    )
    assert evaluate(ctx, rules=RULES) is Action.ADVANCE_LABEL_TO_MERGING_PLAN


def test_advance_label_to_merging_plan_skipped_when_auto_merge_spec_false(
    tmp_path: Path,
) -> None:
    """auto_merge_spec=False parks the ticket at plan-approved — the
    label-advance must not fire (operator-driven merge path)."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:plan-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
        auto_merge_spec=False,
    )
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_MERGING_PLAN


def test_advance_label_to_merging_plan_skipped_when_merging_label_already_present(
    tmp_path: Path,
) -> None:
    """Idempotence: once ``merging-plan`` is on the issue, the label-advance
    must NOT re-fire (otherwise it would loop, since plan-approved persists
    through the merging-plan phase). The attempt_merge_plan rule is what
    fires from here on."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:plan-approved", "foreman:merging-plan")),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
        auto_merge_spec=True,
    )
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_MERGING_PLAN


def test_attempt_merge_plan_fires_on_merging_plan_label_with_open_spec_pr(
    tmp_path: Path,
) -> None:
    """Once merging-plan is set + the spec PR is still open + not merged,
    the attempt_merge_plan rule fires on every tick. The handler reads
    live mergeStateStatus and branches; the rule itself has no CI-state
    predicates because the handler does the per-tick state read."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:plan-approved", "foreman:merging-plan")),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS"),
        auto_merge_spec=True,
    )
    assert evaluate(ctx, rules=RULES) is Action.ATTEMPT_MERGE_PLAN


def test_attempt_merge_plan_skipped_on_merged_spec_pr(tmp_path: Path) -> None:
    """Once the spec PR is merged, ``ctx.pr.is_merged`` flips True and the
    attempt_merge_plan rule's predicate returns False. Without this gate,
    the rule would re-fire ``host.merge_pr`` on an already-merged PR."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:plan-approved", "foreman:merging-plan")),
        _pr(is_merged=True),
    )
    assert evaluate(ctx, rules=RULES) is not Action.ATTEMPT_MERGE_PLAN


def test_advance_label_to_merging_impl_fires_on_impl_approved_with_open_impl_pr_and_auto_merge_impl(
    tmp_path: Path,
) -> None:
    """Impl-side symmetric: the one-shot label transition fires when
    impl-approved + open impl PR + auto_merge_impl=True."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143"),
        auto_merge_impl=True,
    )
    assert evaluate(ctx, rules=RULES) is Action.ADVANCE_LABEL_TO_MERGING_IMPL


def test_advance_label_to_merging_impl_skipped_when_auto_merge_impl_false(
    tmp_path: Path,
) -> None:
    """auto_merge_impl=False parks the ticket at impl-approved — the
    label-advance must not fire."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved",)),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143"),
        auto_merge_impl=False,
    )
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_MERGING_IMPL


def test_advance_label_to_merging_impl_skipped_when_merging_label_already_present(
    tmp_path: Path,
) -> None:
    """Idempotence: once ``merging-impl`` is on the issue, the label-advance
    must not re-fire — same as the spec side."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved", "foreman:merging-impl")),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143"),
        auto_merge_impl=True,
    )
    assert evaluate(ctx, rules=RULES) is not Action.ADVANCE_LABEL_TO_MERGING_IMPL


def test_attempt_merge_impl_fires_on_merging_impl_label_with_open_impl_pr(
    tmp_path: Path,
) -> None:
    """Once merging-impl is set + impl PR open + not merged, the
    attempt_merge_impl rule fires per tick. Mirrors the spec-side test."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved", "foreman:merging-impl")),
        _pr(mergeable="MERGEABLE", ci_status="SUCCESS", head_ref="foreman/impl-143"),
        auto_merge_impl=True,
    )
    assert evaluate(ctx, rules=RULES) is Action.ATTEMPT_MERGE_IMPL


def test_attempt_merge_impl_skipped_on_merged_impl_pr(tmp_path: Path) -> None:
    """Once the impl PR is merged, the rule stops firing. The
    ``advance_label_to_done`` lagging rule then drives the
    impl-approved → done transition on the next tick."""
    from foreman.reconciler.rules import RULES
    ctx = _ctx_with(
        tmp_path,
        _issue(labels=("foreman:impl-approved", "foreman:merging-impl")),
        _pr(is_merged=True, head_ref="foreman/impl-143"),
    )
    assert evaluate(ctx, rules=RULES) is not Action.ATTEMPT_MERGE_IMPL
