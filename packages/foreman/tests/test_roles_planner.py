"""Integration test for run_planner with mocked provider + mocked PyGithub.

A real-engine integration test (against actual Anthropic API + real GitHub)
is gated behind the `real_engine` pytest marker and lives separately. This
test verifies the orchestration wiring: issue parsing, worktree creation,
provider invocation, label advancement.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from foreman.config import BotConfig, Config, ProjectConfig
from foreman.roles.planner import parse_issue_url, run_planner


def test_parse_issue_url_extracts_owner_repo_number() -> None:
    owner, repo, number = parse_issue_url("https://github.com/jeffrichley/voice/issues/42")
    assert owner == "jeffrichley"
    assert repo == "voice"
    assert number == 42


def test_parse_issue_url_rejects_non_issue_url() -> None:
    with pytest.raises(ValueError, match="Not a GitHub issue URL"):
        parse_issue_url("https://github.com/jeffrichley/voice/pull/42")


@pytest.mark.asyncio
async def test_run_planner_dispatches_and_advances_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Set up config with the tmp_path as the local clone
    clone = tmp_path / "clone"
    clone.mkdir()
    # Init a minimal git repo there
    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=clone, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=clone,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=clone, check=True, capture_output=True
    )
    (clone / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=clone, check=True, capture_output=True)

    monkeypatch.setenv("FOREMAN_PLANNER_BOT_TOKEN", "fake-token")

    cfg = Config(
        projects={
            "voice": ProjectConfig(
                repo="jeffrichley/voice",
                local_clone_path=str(clone),
                bots=BotConfig(planner_env="FOREMAN_PLANNER_BOT_TOKEN"),
            )
        }
    )

    fake_issue = MagicMock()
    fake_issue.body = "Add SSML support to madrigal."
    fake_issue.title = "SSML"
    fake_issue.labels = []
    fake_issue.add_to_labels = MagicMock()
    fake_issue.remove_from_labels = MagicMock()

    fake_repo = MagicMock()
    fake_repo.get_issue.return_value = fake_issue
    fake_repo.default_branch = "main"

    fake_gh = MagicMock()
    fake_gh.get_repo.return_value = fake_repo

    fake_provider = MagicMock()
    fake_provider.run_agent = AsyncMock(
        return_value={
            "pr_url": "https://github.com/jeffrichley/voice/pull/99",
            "pr_number": 99,
            "branch_name": "foreman/issue-42",
            "summary": "Drafted SSML support spec",
            "considered_alternatives": [],
            "confidence": "high",
        }
    )

    with patch("foreman.roles.planner.IdentityRegistry") as mock_reg:
        mock_reg.return_value.get_client.return_value = fake_gh
        result = await run_planner(
            issue_url="https://github.com/jeffrichley/voice/issues/42",
            config=cfg,
            project_name="voice",
            worktrees_root=tmp_path / "worktrees",
            provider=fake_provider,
        )

    assert result.pr_number == 99
    fake_provider.run_agent.assert_called_once()
    fake_issue.add_to_labels.assert_called_with("foreman:spec-review")
    fake_issue.remove_from_labels.assert_called_with("foreman:plan")
