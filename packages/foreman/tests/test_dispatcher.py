"""Tests for the pure-function state machine."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from foreman.config import AppsConfig, ProjectConfig
from foreman.dispatcher import (
    Action,
    ActionKind,
    Ticket,
    is_blocked,
    next_action,
    stage_index,
)


def test_ticket_is_hashable_and_frozen() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    t = Ticket(
        project_name="voice",
        issue_number=42,
        labels=frozenset({"foreman:plan"}),
        last_transition_at=now,
    )
    s = {t}  # hashable
    assert t in s
    with pytest.raises(AttributeError):
        t.issue_number = 99  # type: ignore[misc]


def test_action_kinds_exist() -> None:
    assert ActionKind.RUN_PLANNER.value == "run_planner"
    assert ActionKind.RUN_REVIEWER_SPEC.value == "run_reviewer_spec"
    assert ActionKind.RUN_REVIEWER_IMPL.value == "run_reviewer_impl"
    assert ActionKind.RUN_FIXER_SPEC.value == "run_fixer_spec"
    assert ActionKind.RUN_FIXER_IMPL.value == "run_fixer_impl"
    assert ActionKind.RUN_WORKER.value == "run_worker"
    assert ActionKind.MERGE_SPEC_PR.value == "merge_spec_pr"
    assert ActionKind.MERGE_IMPL_PR.value == "merge_impl_pr"


def test_stage_index_orders_pipeline_progress() -> None:
    # Higher index = further along
    assert stage_index(ActionKind.RUN_PLANNER) < stage_index(ActionKind.RUN_REVIEWER_SPEC)
    assert stage_index(ActionKind.RUN_REVIEWER_SPEC) < stage_index(ActionKind.RUN_FIXER_SPEC)
    assert stage_index(ActionKind.RUN_FIXER_SPEC) < stage_index(ActionKind.MERGE_SPEC_PR)
    assert stage_index(ActionKind.MERGE_SPEC_PR) < stage_index(ActionKind.RUN_WORKER)
    assert stage_index(ActionKind.RUN_WORKER) < stage_index(ActionKind.RUN_REVIEWER_IMPL)
    assert stage_index(ActionKind.RUN_REVIEWER_IMPL) < stage_index(ActionKind.RUN_FIXER_IMPL)
    assert stage_index(ActionKind.RUN_FIXER_IMPL) < stage_index(ActionKind.MERGE_IMPL_PR)


def _ticket(labels: set[str]) -> Ticket:
    return Ticket(
        project_name="voice",
        issue_number=42,
        labels=frozenset(labels),
        last_transition_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def test_is_blocked_true_when_hold_label_present() -> None:
    assert is_blocked(_ticket({"foreman:plan", "foreman:hold"})) is True


def test_is_blocked_true_when_failed_label_present() -> None:
    assert is_blocked(_ticket({"foreman:plan", "foreman:failed"})) is True


def test_is_blocked_false_when_neither_present() -> None:
    assert is_blocked(_ticket({"foreman:plan"})) is False


def test_is_blocked_false_when_only_workflow_labels_present() -> None:
    assert is_blocked(_ticket({"foreman:spec-review"})) is False


def _project(*, auto_merge_spec: bool = False, auto_merge_impl: bool = False) -> ProjectConfig:
    return ProjectConfig(
        repo="jeffrichley/voice",
        local_clone_path="/tmp/voice",
        apps=AppsConfig(),
        auto_merge_spec=auto_merge_spec,
        auto_merge_impl=auto_merge_impl,
    )


def test_next_action_plan_label_runs_planner() -> None:
    action = next_action(_ticket({"foreman:plan"}), _project())
    assert action == Action(kind=ActionKind.RUN_PLANNER)


def test_next_action_planning_label_returns_none() -> None:
    assert next_action(_ticket({"foreman:planning"}), _project()) is None


def test_next_action_spec_review_runs_reviewer_spec() -> None:
    action = next_action(_ticket({"foreman:spec-review"}), _project())
    assert action == Action(kind=ActionKind.RUN_REVIEWER_SPEC)


def test_next_action_spec_fix_runs_fixer_spec() -> None:
    action = next_action(_ticket({"foreman:spec-fix"}), _project())
    assert action == Action(kind=ActionKind.RUN_FIXER_SPEC)


def test_next_action_spec_ready_no_auto_merge_returns_none() -> None:
    assert next_action(_ticket({"foreman:spec-ready"}), _project(auto_merge_spec=False)) is None


def test_next_action_spec_ready_with_auto_merge_merges_spec_pr() -> None:
    action = next_action(_ticket({"foreman:spec-ready"}), _project(auto_merge_spec=True))
    assert action == Action(kind=ActionKind.MERGE_SPEC_PR)


def test_next_action_implementing_label_returns_none() -> None:
    assert next_action(_ticket({"foreman:implementing"}), _project()) is None


def test_next_action_impl_review_runs_reviewer_impl() -> None:
    action = next_action(_ticket({"foreman:impl-review"}), _project())
    assert action == Action(kind=ActionKind.RUN_REVIEWER_IMPL)


def test_next_action_impl_fix_runs_fixer_impl() -> None:
    action = next_action(_ticket({"foreman:impl-fix"}), _project())
    assert action == Action(kind=ActionKind.RUN_FIXER_IMPL)


def test_next_action_ready_for_merge_no_auto_merge_returns_none() -> None:
    assert (
        next_action(_ticket({"foreman:ready-for-merge"}), _project(auto_merge_impl=False)) is None
    )


def test_next_action_ready_for_merge_with_auto_merge_merges_impl_pr() -> None:
    action = next_action(_ticket({"foreman:ready-for-merge"}), _project(auto_merge_impl=True))
    assert action == Action(kind=ActionKind.MERGE_IMPL_PR)


def test_next_action_implementing_ready_label_runs_worker() -> None:
    action = next_action(_ticket({"foreman:implementing-ready"}), _project())
    assert action == Action(kind=ActionKind.RUN_WORKER)


def test_next_action_hold_label_returns_none_despite_plan() -> None:
    assert next_action(_ticket({"foreman:plan", "foreman:hold"}), _project()) is None


def test_next_action_failed_label_returns_none_despite_spec_review() -> None:
    assert next_action(_ticket({"foreman:spec-review", "foreman:failed"}), _project()) is None


def test_next_action_no_foreman_labels_returns_none() -> None:
    assert next_action(_ticket({"bug", "enhancement"}), _project()) is None
