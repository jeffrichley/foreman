"""Typer role commands delegate to run_<role>_cli."""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from foreman.v4.cli import app


def test_plan_command_invokes_run_planner_cli():
    with patch("foreman.v4.cli.run_planner_cli", return_value=0) as mock:
        result = CliRunner().invoke(
            app,
            ["plan", "--project", "p", "--issue-number", "1"],
        )
        assert result.exit_code == 0
        mock.assert_called_once_with(project="p", issue_number=1)


def test_review_command_passes_target():
    with patch("foreman.v4.cli.run_reviewer_cli", return_value=0) as mock:
        CliRunner().invoke(
            app,
            ["review", "--project", "p", "--issue-number", "1", "--target", "spec"],
        )
        mock.assert_called_once_with(project="p", issue_number=1, target="spec")


def test_implement_command_invokes_run_worker_cli():
    with patch("foreman.v4.cli.run_worker_cli", return_value=0) as mock:
        CliRunner().invoke(
            app,
            ["implement", "--project", "p", "--issue-number", "1"],
        )
        mock.assert_called_once_with(project="p", issue_number=1)


def test_fix_command_passes_target():
    with patch("foreman.v4.cli.run_fixer_cli", return_value=0) as mock:
        CliRunner().invoke(
            app,
            ["fix", "--project", "p", "--issue-number", "1", "--target", "impl"],
        )
        mock.assert_called_once_with(project="p", issue_number=1, target="impl")
