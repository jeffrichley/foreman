"""Fixer v4 CLI emits FOREMAN_OUTCOME on exit (target-aware).

The Fixer's outcome surface is narrower than the Reviewer's: the
caller only cares whether the fix was pushed (``CLEAN``), whether the
Fixer escalated after exhausting attempts (``NEEDS_HELP``), or whether
something raised (``ERROR``). Findings are NOT propagated — the
downstream v4 state machine routes the amended PR back to the
Reviewer, which re-emits its own NEEDS_FIX/CLEAN verdict.

Tests mirror the Reviewer's pattern (Task 5.3): parametrize on the v4
``target`` literal so the CLI's target-aware contract is exercised in
both spec and impl shape with a single test body.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from foreman.roles.fixer import run_fixer_cli
from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout


def _pushed_result(pr_number: int):
    """v4-shaped result for a fix pushed to the amended PR.

    Mirrors the flat-shape adapter ``_run_fixer_for_v4`` builds from
    the legacy ``FixerRunResult``. ``run_fixer_cli`` consumes these
    attributes via ``getattr``, so a MagicMock with the right attribute
    names is enough — no real Pydantic instance required.
    """
    r = MagicMock()
    r.pushed = True
    r.escalated = False
    r.pr_number = pr_number
    r.summary = "amended"
    return r


def _escalated_result():
    """v4-shaped result for a Fixer escalation (max attempts hit).

    ``pushed`` is False — there was no successful amend — and
    ``escalated`` is True so ``run_fixer_cli`` emits NEEDS_HELP rather
    than CLEAN. The downstream v4 state machine surfaces this to a
    human-intervention queue.
    """
    r = MagicMock()
    r.pushed = False
    r.escalated = True
    r.pr_number = None
    r.summary = "3 attempts exhausted"
    return r


@pytest.mark.parametrize("target", ["spec", "impl"])
def test_pushed_emits_clean(target, capsys):
    """Fixer pushed → CLEAN with pr_number on artifacts."""
    with patch(
        "foreman.roles.fixer._run_fixer_for_v4",
        return_value=_pushed_result(11),
    ):
        exit_code = run_fixer_cli(project="p", issue_number=1, target=target)
    assert exit_code == 0
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.CLEAN
    assert outcome.artifacts.pr_number == 11


@pytest.mark.parametrize("target", ["spec", "impl"])
def test_escalated_emits_needs_help(target, capsys):
    """Fixer escalated → NEEDS_HELP (no findings propagated)."""
    with patch(
        "foreman.roles.fixer._run_fixer_for_v4",
        return_value=_escalated_result(),
    ):
        exit_code = run_fixer_cli(project="p", issue_number=1, target=target)
    assert exit_code == 0  # zero exit even on NEEDS_HELP — stdout carries the verdict
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.NEEDS_HELP


def test_fixer_exception_emits_error(capsys):
    """Fixer raised → ERROR + exit 1."""
    with patch(
        "foreman.roles.fixer._run_fixer_for_v4",
        side_effect=RuntimeError("push rejected"),
    ):
        exit_code = run_fixer_cli(project="p", issue_number=1, target="spec")
    assert exit_code == 1
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.ERROR
    assert "push rejected" in outcome.summary


def test_fixer_transient_outcome(capsys):
    """foreman#361: ``ProviderTransientError`` from the fixer core
    emits ``TRANSIENT_PROVIDER_ERROR`` with details populated, AND
    exits 0 (the outcome JSON carries the signal; non-zero would
    trip ``RoleSubprocessError`` in the dispatcher and lose the
    discriminator).
    """
    from foreman.providers import ProviderTransientError

    original_cause = Exception("Claude Code returned an error result: 429 Too Many Requests")
    transient = ProviderTransientError(str(original_cause))
    transient.__cause__ = original_cause
    with patch(
        "foreman.roles.fixer._run_fixer_for_v4",
        side_effect=transient,
    ):
        exit_code = run_fixer_cli(project="p", issue_number=1, target="impl")
    assert exit_code == 0
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR
    assert "429" in outcome.details["provider_status"]
    assert outcome.details["exception_class"] == "Exception"


# --- Phase 8d.17 / foreman#315 ---------------------------------------


def test_fixer_run_fixer_cli_populates_details_with_fix_comment(capsys):
    """CLEAN path carries FixerOutput fix_comment + addressed counts
    in details so operators can see what was addressed without
    pulling the PR comment."""
    pushed = MagicMock()
    pushed.pushed = True
    pushed.escalated = False
    pushed.pr_number = 11
    pushed.summary = "amended"
    pushed.details = {
        "fix_comment": "Addressed finding 1 by adding the missing test.",
        "outcome": "fixed",
        "confidence": "high",
        "commits_made": [
            {
                "sha": "def456",
                "summary": "test: cover happy path",
                "findings_addressed": ["foo.py:42"],
            },
        ],
        "addressed_findings": [{"target": "foo.py:42", "summary": "added test"}],
        "unaddressed_findings": [],
    }
    with patch(
        "foreman.roles.fixer._run_fixer_for_v4",
        return_value=pushed,
    ):
        exit_code = run_fixer_cli(project="p", issue_number=1, target="impl")
    assert exit_code == 0
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.CLEAN
    assert outcome.details["outcome"] == "fixed"
    assert "missing test" in outcome.details["fix_comment"]
    assert outcome.details["commits_made"][0]["sha"] == "def456"
