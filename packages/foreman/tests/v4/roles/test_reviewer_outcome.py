"""Reviewer v4 CLI emits FOREMAN_OUTCOME on exit (target-aware)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from foreman.roles.reviewer import run_reviewer_cli
from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout


def _approved_result(pr_number: int):
    """v4-shaped result for an approved review.

    Mirrors the flat-shape adapter that ``_run_reviewer_for_v4`` builds
    from the legacy ``ReviewerOutput``. ``run_reviewer_cli`` consumes
    these attributes via ``getattr``, so a MagicMock with the right
    attribute names is enough — no real Pydantic instance required.
    """
    r = MagicMock()
    r.approved = True
    r.pr_number = pr_number
    r.summary = "looks good"
    r.findings = []
    return r


def _rejected_result(pr_number: int):
    """v4-shaped result for a needs_fix review with one finding.

    Each finding carries v4-Finding fields (``severity`` /
    ``location`` / ``description``) because ``_run_reviewer_for_v4``
    translates the legacy ``Finding`` (``severity`` / ``target`` /
    ``issue`` / ``needed``) into v4 shape before returning.
    """
    finding = MagicMock()
    finding.severity = "important"
    finding.location = "foo.py:42"
    finding.description = "missing test"
    r = MagicMock()
    r.approved = False
    r.pr_number = pr_number
    r.summary = "1 important issue"
    r.findings = [finding]
    return r


@pytest.mark.parametrize("target", ["spec", "impl"])
def test_approved_emits_clean(target, capsys):
    """Reviewer approves → CLEAN with pr_number on artifacts."""
    with patch(
        "foreman.roles.reviewer._run_reviewer_for_v4",
        return_value=_approved_result(7),
    ):
        exit_code = run_reviewer_cli(project="p", issue_number=1, target=target)
    assert exit_code == 0
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.CLEAN
    assert outcome.artifacts.pr_number == 7


@pytest.mark.parametrize("target", ["spec", "impl"])
def test_changes_requested_emits_needs_fix_with_findings(target, capsys):
    """Reviewer rejects → NEEDS_FIX carrying the structured findings."""
    with patch(
        "foreman.roles.reviewer._run_reviewer_for_v4",
        return_value=_rejected_result(7),
    ):
        exit_code = run_reviewer_cli(project="p", issue_number=1, target=target)
    assert exit_code == 0  # zero exit even on NEEDS_FIX — stdout carries the verdict
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.NEEDS_FIX
    assert len(outcome.findings) == 1
    assert outcome.findings[0].location == "foo.py:42"


def test_reviewer_exception_emits_error(capsys):
    """Reviewer raised → ERROR + exit 1."""
    with patch(
        "foreman.roles.reviewer._run_reviewer_for_v4",
        side_effect=RuntimeError("rate limit"),
    ):
        exit_code = run_reviewer_cli(project="p", issue_number=1, target="spec")
    assert exit_code == 1
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.ERROR
    assert "rate limit" in outcome.summary


def test_reviewer_transient_outcome(capsys):
    """foreman#361: ``ProviderTransientError`` from the reviewer core
    emits ``TRANSIENT_PROVIDER_ERROR`` with details populated, AND
    exits 0 (the outcome JSON carries the signal; non-zero would trip
    ``RoleSubprocessError`` in the dispatcher and lose the
    discriminator).
    """
    from foreman.providers import ProviderTransientError

    original_cause = Exception("Claude Code returned an error result: 503 Service Unavailable")
    transient = ProviderTransientError(str(original_cause))
    transient.__cause__ = original_cause
    with patch(
        "foreman.roles.reviewer._run_reviewer_for_v4",
        side_effect=transient,
    ):
        exit_code = run_reviewer_cli(project="p", issue_number=1, target="spec")
    assert exit_code == 0
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR
    assert "503" in outcome.details["provider_status"]
    assert outcome.details["exception_class"] == "Exception"


# --- Phase 8d.17 / foreman#315 ---------------------------------------


def test_reviewer_run_reviewer_cli_populates_details_with_findings(capsys):
    """NEEDS_FIX path carries the Reviewer's pr_review_comment in
    details (findings are already on Outcome.findings; we add the
    prose so /foreman show + the event archive carry forward the
    body the Reviewer posted on the PR)."""
    finding = MagicMock()
    finding.severity = "important"
    finding.location = "foo.py:42"
    finding.description = "missing test"
    rejected = MagicMock()
    rejected.approved = False
    rejected.pr_number = 7
    rejected.summary = "1 important issue"
    rejected.findings = [finding]
    rejected.details = {
        "pr_review_comment": (
            "Needs fix: the spec calls for a test on the happy path but no "
            "test covers it. See finding 1."
        ),
        "outcome": "needs_fix",
        "confidence": "high",
    }
    with patch(
        "foreman.roles.reviewer._run_reviewer_for_v4",
        return_value=rejected,
    ):
        exit_code = run_reviewer_cli(project="p", issue_number=1, target="impl")
    assert exit_code == 0
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.NEEDS_FIX
    assert outcome.details["outcome"] == "needs_fix"
    assert "no test covers it" in outcome.details["pr_review_comment"]
