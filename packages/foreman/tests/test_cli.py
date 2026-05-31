"""CLI smoke tests via click's testing harness."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from foreman.cli import cli
from foreman.git_host import PRRef
from foreman.schemas.planner import PlannerOutput, PlannerRunResult
from foreman.schemas.reviewer import Finding, ReviewerOutput


def test_cli_plan_invokes_run_planner(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id_env = "FOREMAN_PLANNER_APP_ID"
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
"""
    )

    fake_result = PlannerRunResult(
        llm_output=PlannerOutput(
            spec_doc_content="# Spec",
            pr_title="spec: x",
            pr_body="body",
            summary="ok",
            considered_alternatives=[],
            confidence="medium",
        ),
        pr=PRRef(
            number=99,
            url="https://github.com/jeffrichley/voice/pull/99",
            title="spec: x",
            body="body",
            branch="foreman/issue-42",
            base_branch="main",
            repo_slug="jeffrichley/voice",
        ),
    )

    runner = CliRunner()
    with patch("foreman.cli.run_planner", new=AsyncMock(return_value=fake_result)) as mock_run:
        result = runner.invoke(
            cli,
            [
                "plan",
                "https://github.com/jeffrichley/voice/issues/42",
                "--project",
                "voice",
                "--config",
                str(config_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "PR #99" in result.output or "pull/99" in result.output
    assert "foreman/issue-42" in result.output
    mock_run.assert_called_once()


def test_cli_help_lists_plan_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "plan" in result.output


def test_cli_help_lists_review_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "review" in result.output


def test_cli_review_invokes_run_reviewer(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[projects.voice]
repo = "jeffrichley/voice"
local_clone_path = "/tmp/voice"

[projects.voice.apps]
planner_app_id_env = "FOREMAN_PLANNER_APP_ID"
planner_app_id = 123456
planner_private_key_path = "/tmp/planner.pem"
reviewer_app_id_env = "FOREMAN_REVIEWER_APP_ID"
reviewer_app_id = 654321
reviewer_private_key_path = "/tmp/reviewer.pem"
"""
    )

    fake_result = ReviewerOutput(
        outcome="needs_fix",
        review_comment="needs_fix — see findings.",
        findings=[
            Finding(
                severity="important",
                target="Acceptance criteria bullet 3",
                issue="Uses 'improve' which is not testable.",
                needed="Replace with a concrete verb.",
            )
        ],
        confidence="medium",
    )

    runner = CliRunner()
    with patch(
        "foreman.cli.run_reviewer", new=AsyncMock(return_value=fake_result)
    ) as mock_run:
        result = runner.invoke(
            cli,
            [
                "review",
                "https://github.com/jeffrichley/voice/pull/77",
                "--project",
                "voice",
                "--config",
                str(config_file),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "needs_fix" in result.output
    assert "1 findings" in result.output
    assert "confidence=medium" in result.output
    mock_run.assert_called_once()
