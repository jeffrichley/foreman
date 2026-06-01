"""Tests for DaemonRunners — wraps role functions for the daemon."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from foreman.config import AdminConfig, AppsConfig, Config, DaemonConfig, ProjectConfig
from foreman.daemon_runners import DaemonRunners
from foreman.dispatcher import Ticket


def _ticket(issue: int = 42, labels: set[str] | None = None) -> Ticket:
    return Ticket(
        project_name="voice",
        issue_number=issue,
        labels=frozenset(labels or {"foreman:plan"}),
        last_transition_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def _config(tmp_path: Path) -> Config:
    return Config(
        admin=AdminConfig(),
        daemon=DaemonConfig(sqlite_path=str(tmp_path / "f.sqlite")),
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path=str(tmp_path / "voice"),
                apps=AppsConfig(),
            )
        },
    )


@pytest.fixture
def host() -> MagicMock:
    """Fake GitHubDaemonHost with the methods the runners use."""
    m = MagicMock()
    m.get_issue_labels = MagicMock(return_value=["foreman:spec-review"])
    m.find_pr_for_branch = MagicMock(return_value=18)
    m.merge_pull_request = MagicMock()
    m.add_issue_label = MagicMock()
    m.remove_issue_label = MagicMock()
    m.close_issue = MagicMock()
    return m


@pytest.mark.asyncio
async def test_run_planner_calls_role_with_issue_url(tmp_path: Path, host: MagicMock) -> None:
    config = _config(tmp_path)
    mock_role = AsyncMock(return_value=MagicMock(model_dump=lambda: {"pr": 18}))

    runners = DaemonRunners(
        host=host,
        worktrees_root=tmp_path / "worktrees",
        _planner=mock_role,
    )

    result = await runners.run_planner(ticket=_ticket(), config=config)

    mock_role.assert_awaited_once()
    kwargs = mock_role.await_args.kwargs
    assert kwargs["issue_url"] == "https://github.com/jeffrichley/voice/issues/42"
    assert kwargs["project_name"] == "voice"
    # New labels are read back from the host after the role advances them.
    assert "foreman:spec-review" in result.new_labels


@pytest.mark.asyncio
async def test_run_reviewer_with_spec_target_calls_role_with_spec_pr_url(
    tmp_path: Path, host: MagicMock
) -> None:
    config = _config(tmp_path)
    mock_role = AsyncMock(return_value=MagicMock(model_dump=lambda: {}))
    host.find_pr_for_branch.return_value = 99

    runners = DaemonRunners(
        host=host,
        worktrees_root=tmp_path / "worktrees",
        _reviewer=mock_role,
    )

    await runners.run_reviewer(ticket=_ticket(), config=config, target="spec_pr")

    kwargs = mock_role.await_args.kwargs
    assert kwargs["pr_url"] == "https://github.com/jeffrichley/voice/pull/99"
    # spec_pr target → looks up PR for branch foreman/issue-42
    host.find_pr_for_branch.assert_called_once_with("jeffrichley/voice", "foreman/issue-42")


@pytest.mark.asyncio
async def test_run_reviewer_with_impl_target_uses_impl_branch(
    tmp_path: Path, host: MagicMock
) -> None:
    config = _config(tmp_path)
    mock_role = AsyncMock(return_value=MagicMock(model_dump=lambda: {}))
    host.find_pr_for_branch.return_value = 100

    runners = DaemonRunners(
        host=host,
        worktrees_root=tmp_path / "worktrees",
        _reviewer=mock_role,
    )

    await runners.run_reviewer(ticket=_ticket(), config=config, target="impl_pr")

    # impl_pr target → looks up PR for branch foreman/issue-42-impl
    host.find_pr_for_branch.assert_called_once_with(
        "jeffrichley/voice", "foreman/issue-42-impl"
    )


@pytest.mark.asyncio
async def test_run_reviewer_raises_when_no_pr_for_branch(
    tmp_path: Path, host: MagicMock
) -> None:
    config = _config(tmp_path)
    mock_role = AsyncMock()
    host.find_pr_for_branch.return_value = None

    runners = DaemonRunners(
        host=host,
        worktrees_root=tmp_path / "worktrees",
        _reviewer=mock_role,
    )

    with pytest.raises(RuntimeError, match="No open PR found for branch"):
        await runners.run_reviewer(ticket=_ticket(), config=config, target="spec_pr")
    # Role NOT called because we couldn't find the PR.
    mock_role.assert_not_called()


@pytest.mark.asyncio
async def test_merge_spec_pr_merges_and_sets_implementing_ready(
    tmp_path: Path, host: MagicMock
) -> None:
    config = _config(tmp_path)
    host.find_pr_for_branch.return_value = 18
    host.get_issue_labels.return_value = ["foreman:implementing-ready"]

    runners = DaemonRunners(host=host, worktrees_root=tmp_path / "worktrees")

    result = await runners.merge_spec_pr(ticket=_ticket(), config=config)

    host.find_pr_for_branch.assert_called_once_with("jeffrichley/voice", "foreman/issue-42")
    host.merge_pull_request.assert_called_once_with("jeffrichley/voice", 18)
    host.add_issue_label.assert_called_once_with(
        "jeffrichley/voice", 42, "foreman:implementing-ready"
    )
    # Remove any prior spec-state labels that linger.
    host.remove_issue_label.assert_any_call("jeffrichley/voice", 42, "foreman:spec-ready")
    assert "foreman:implementing-ready" in result.new_labels


@pytest.mark.asyncio
async def test_merge_impl_pr_merges_and_closes_issue(
    tmp_path: Path, host: MagicMock
) -> None:
    config = _config(tmp_path)
    host.find_pr_for_branch.return_value = 25
    host.get_issue_labels.return_value = []

    runners = DaemonRunners(host=host, worktrees_root=tmp_path / "worktrees")

    result = await runners.merge_impl_pr(ticket=_ticket(), config=config)

    host.find_pr_for_branch.assert_called_once_with(
        "jeffrichley/voice", "foreman/issue-42-impl"
    )
    host.merge_pull_request.assert_called_once_with("jeffrichley/voice", 25)
    host.close_issue.assert_called_once_with("jeffrichley/voice", 42)
    assert result.new_labels == frozenset()


@pytest.mark.asyncio
async def test_merge_spec_pr_raises_when_no_pr_found(
    tmp_path: Path, host: MagicMock
) -> None:
    config = _config(tmp_path)
    host.find_pr_for_branch.return_value = None

    runners = DaemonRunners(host=host, worktrees_root=tmp_path / "worktrees")

    with pytest.raises(RuntimeError, match="No open spec PR"):
        await runners.merge_spec_pr(ticket=_ticket(), config=config)
    host.merge_pull_request.assert_not_called()
