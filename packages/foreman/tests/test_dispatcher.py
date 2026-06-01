"""Tests for the pure-function state machine."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foreman.dispatcher import (
    Action,
    ActionKind,
    Ticket,
    stage_index,
)


def test_ticket_is_hashable_and_frozen() -> None:
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
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
