"""Tests for the real-role dispatcher that wires Action → role function."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from foreman.config import AppsConfig, Config, DaemonConfig, ProjectConfig
from foreman.dispatcher import Action, ActionKind, Ticket
from foreman.role_dispatch import RealRoleDispatcher


def _ticket(labels: set[str]) -> Ticket:
    return Ticket(
        project_name="voice",
        issue_number=42,
        labels=frozenset(labels),
        last_transition_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def _config() -> Config:
    return Config(
        daemon=DaemonConfig(sqlite_path="/tmp/f.sqlite"),
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path="/tmp/voice",
                apps=AppsConfig(),
            )
        },
    )


@pytest.mark.asyncio
async def test_dispatch_run_planner_routes_to_planner_run() -> None:
    config = _config()
    runners = MagicMock()
    runners.run_planner = AsyncMock(
        return_value=MagicMock(new_labels=["foreman:spec-review"], structured_output={"x": 1})
    )

    dispatcher = RealRoleDispatcher(config=config, runners=runners)
    action = Action(kind=ActionKind.RUN_PLANNER)

    result = await dispatcher.dispatch(ticket=_ticket({"foreman:plan"}), action=action)

    runners.run_planner.assert_awaited_once()
    assert result.new_labels == frozenset({"foreman:spec-review"})


@pytest.mark.asyncio
async def test_dispatch_run_reviewer_spec_routes_to_reviewer_with_spec_target() -> None:
    config = _config()
    runners = MagicMock()
    runners.run_reviewer = AsyncMock(
        return_value=MagicMock(new_labels=["foreman:spec-fix"], structured_output=None)
    )

    dispatcher = RealRoleDispatcher(config=config, runners=runners)
    action = Action(kind=ActionKind.RUN_REVIEWER_SPEC)

    await dispatcher.dispatch(ticket=_ticket({"foreman:spec-review"}), action=action)

    runners.run_reviewer.assert_awaited_once()
    kwargs = runners.run_reviewer.await_args.kwargs
    assert kwargs.get("target") == "spec_pr"


@pytest.mark.asyncio
async def test_dispatch_merge_spec_pr_routes_to_host_merge() -> None:
    config = _config()
    runners = MagicMock()
    runners.merge_spec_pr = AsyncMock(
        return_value=MagicMock(new_labels=["foreman:implementing-ready"], structured_output=None)
    )

    dispatcher = RealRoleDispatcher(config=config, runners=runners)
    action = Action(kind=ActionKind.MERGE_SPEC_PR)

    result = await dispatcher.dispatch(ticket=_ticket({"foreman:spec-ready"}), action=action)

    runners.merge_spec_pr.assert_awaited_once()
    assert result.new_labels == frozenset({"foreman:implementing-ready"})
