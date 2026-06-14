"""Planner v4 CLI emits FOREMAN_OUTCOME on exit."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from foreman.roles.planner import run_planner_cli
from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout


def _fake_planner_returns_pr(pr_url: str, pr_number: int, summary: str):
    """Build a fake of whatever the planner-invocation returns internally."""
    result = MagicMock()
    result.pr_url = pr_url
    result.pr_number = pr_number
    result.summary = summary
    result.confidence = "high"
    return result


def test_planner_success_emits_clean_outcome(capsys):
    """Planner opens a spec PR successfully → CLEAN with pr_number + pr_url."""
    fake_result = _fake_planner_returns_pr(
        pr_url="https://github.com/x/y/pull/42",
        pr_number=42,
        summary="spec PR opened",
    )
    with patch(
        "foreman.roles.planner._run_planner_for_v4",
        return_value=fake_result,
    ):
        exit_code = run_planner_cli(project="p", issue_number=1)
    assert exit_code == 0
    captured = capsys.readouterr()
    outcome = parse_outcome_from_stdout(captured.out)
    assert outcome.kind == OutcomeKind.CLEAN
    assert outcome.artifacts.pr_number == 42
    assert outcome.artifacts.pr_url == "https://github.com/x/y/pull/42"


def test_planner_low_confidence_emits_needs_help(capsys):
    """Planner ran but confidence=low → NEEDS_HELP."""
    fake_result = MagicMock()
    fake_result.pr_url = None
    fake_result.pr_number = None
    fake_result.summary = "ticket under-specified"
    fake_result.confidence = "low"
    with patch(
        "foreman.roles.planner._run_planner_for_v4",
        return_value=fake_result,
    ):
        exit_code = run_planner_cli(project="p", issue_number=1)
    assert exit_code == 0  # zero exit even on NEEDS_HELP — stdout carries the verdict
    captured = capsys.readouterr()
    outcome = parse_outcome_from_stdout(captured.out)
    assert outcome.kind == OutcomeKind.NEEDS_HELP


def test_planner_exception_emits_error(capsys):
    """Planner raised → ERROR + exit 1."""
    with patch(
        "foreman.roles.planner._run_planner_for_v4",
        side_effect=RuntimeError("provider timeout"),
    ):
        exit_code = run_planner_cli(project="p", issue_number=1)
    assert exit_code == 1
    captured = capsys.readouterr()
    outcome = parse_outcome_from_stdout(captured.out)
    assert outcome.kind == OutcomeKind.ERROR
    assert "provider timeout" in outcome.summary
