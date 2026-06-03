"""Tests for the FixerOutput + AddressedFinding + UnaddressedFinding +
CommitMade + FixerRunResult Pydantic schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foreman.schemas.fixer import (
    AddressedFinding,
    CommitMade,
    FixerOutput,
    FixerRunResult,
    UnaddressedFinding,
)


def _addressed() -> dict[str, object]:
    return {
        "target": "Acceptance criteria bullet 3",
        "summary": "Replaced 'improve' with 'add unit test for X'.",
    }


def _unaddressed(reason: str = "needs_info") -> dict[str, object]:
    return {
        "target": "Approach section, sub-request 2",
        "severity": "important",
        "reason": reason,
        "rationale": (
            "The issue does not name a concrete throughput target, so no "
            "specific number can be inserted without inventing facts."
        ),
    }


def _commit() -> dict[str, object]:
    return {
        "sha": "a" * 40,
        "summary": "fix: address Reviewer findings on spec 42",
        "findings_addressed": ["Acceptance criteria bullet 3"],
    }


# ----------------------------------------------------------------------
# AddressedFinding
# ----------------------------------------------------------------------


def test_addressed_finding_validates_valid_dict() -> None:
    f = AddressedFinding.model_validate(_addressed())
    assert f.target == "Acceptance criteria bullet 3"


def test_addressed_finding_requires_target_and_summary() -> None:
    bad = _addressed()
    del bad["summary"]
    with pytest.raises(ValidationError, match="summary"):
        AddressedFinding.model_validate(bad)


# ----------------------------------------------------------------------
# UnaddressedFinding
# ----------------------------------------------------------------------


def test_unaddressed_finding_validates_with_needs_info() -> None:
    f = UnaddressedFinding.model_validate(_unaddressed("needs_info"))
    assert f.reason == "needs_info"
    assert f.severity == "important"


def test_unaddressed_finding_validates_with_needed_remediation_wrong() -> None:
    f = UnaddressedFinding.model_validate(_unaddressed("needed_remediation_wrong"))
    assert f.reason == "needed_remediation_wrong"


def test_unaddressed_finding_validates_with_requires_git_surgery() -> None:
    f = UnaddressedFinding.model_validate(_unaddressed("requires_git_surgery"))
    assert f.reason == "requires_git_surgery"


def test_unaddressed_finding_validates_with_requires_worker_codebase_access() -> None:
    f = UnaddressedFinding.model_validate(_unaddressed("requires_worker_codebase_access"))
    assert f.reason == "requires_worker_codebase_access"


def test_unaddressed_finding_rejects_unknown_reason() -> None:
    bad = _unaddressed()
    bad["reason"] = "deferred-to-human"
    with pytest.raises(ValidationError, match="reason"):
        UnaddressedFinding.model_validate(bad)


def test_unaddressed_finding_rejects_unknown_severity() -> None:
    bad = _unaddressed()
    bad["severity"] = "blocker"
    with pytest.raises(ValidationError, match="severity"):
        UnaddressedFinding.model_validate(bad)


def test_unaddressed_finding_requires_rationale() -> None:
    bad = _unaddressed()
    del bad["rationale"]
    with pytest.raises(ValidationError, match="rationale"):
        UnaddressedFinding.model_validate(bad)


# ----------------------------------------------------------------------
# CommitMade
# ----------------------------------------------------------------------


def test_commit_made_validates_valid_dict() -> None:
    c = CommitMade.model_validate(_commit())
    assert c.sha == "a" * 40
    assert c.findings_addressed == ["Acceptance criteria bullet 3"]


def test_commit_made_findings_addressed_default_empty() -> None:
    c = CommitMade.model_validate({"sha": "b" * 40, "summary": "ad-hoc"})
    assert c.findings_addressed == []


# ----------------------------------------------------------------------
# FixerOutput — minimal / fixed / incomplete
# ----------------------------------------------------------------------


def _minimal_fixed() -> dict[str, object]:
    return {
        "outcome": "fixed",
        "fix_comment": "fixed — addressed all critical+important findings.",
        "commits_made": [_commit()],
        "addressed_findings": [_addressed()],
        "unaddressed_findings": [],
        "confidence": "high",
    }


def _minimal_incomplete() -> dict[str, object]:
    return {
        "outcome": "incomplete",
        "fix_comment": "incomplete — one critical finding needs human review.",
        "commits_made": [],
        "addressed_findings": [],
        "unaddressed_findings": [_unaddressed()],
    }


def test_fixer_output_fixed_round_trips() -> None:
    obj = FixerOutput.model_validate(_minimal_fixed())
    assert obj.outcome == "fixed"
    assert len(obj.addressed_findings) == 1
    assert obj.confidence == "high"


def test_fixer_output_incomplete_with_unaddressed_findings() -> None:
    obj = FixerOutput.model_validate(_minimal_incomplete())
    assert obj.outcome == "incomplete"
    assert len(obj.unaddressed_findings) == 1
    assert obj.confidence == "medium"  # default


def test_fixer_output_rejects_bad_outcome() -> None:
    bad = _minimal_fixed()
    bad["outcome"] = "partial"
    with pytest.raises(ValidationError, match="outcome"):
        FixerOutput.model_validate(bad)


def test_fixer_output_rejects_bad_confidence() -> None:
    bad = _minimal_fixed()
    bad["confidence"] = "very-high"
    with pytest.raises(ValidationError, match="confidence"):
        FixerOutput.model_validate(bad)


def test_fixer_output_allows_empty_addressed_and_commits_for_full_skip() -> None:
    """Every-finding-unaddressable is a valid incomplete outcome."""
    data = {
        "outcome": "incomplete",
        "fix_comment": "incomplete — all findings require git surgery.",
        "commits_made": [],
        "addressed_findings": [],
        "unaddressed_findings": [_unaddressed("requires_git_surgery")],
    }
    obj = FixerOutput.model_validate(data)
    assert obj.addressed_findings == []
    assert obj.commits_made == []


def test_fixer_output_json_schema_is_serializable() -> None:
    schema = FixerOutput.model_json_schema()
    assert isinstance(schema, dict)
    assert "properties" in schema
    assert "outcome" in schema["properties"]
    assert "fix_comment" in schema["properties"]
    assert "addressed_findings" in schema["properties"]
    assert "unaddressed_findings" in schema["properties"]


# ----------------------------------------------------------------------
# FixerRunResult
# ----------------------------------------------------------------------


def test_fixer_run_result_bundles_output_and_attempt() -> None:
    output = FixerOutput.model_validate(_minimal_fixed())
    result = FixerRunResult(
        llm_output=output,
        attempt=2,
        final_labels=["foreman:spec-review"],
    )
    assert result.llm_output.outcome == "fixed"
    assert result.attempt == 2
    assert result.final_labels == ["foreman:spec-review"]


def test_fixer_run_result_requires_attempt() -> None:
    output = FixerOutput.model_validate(_minimal_fixed())
    with pytest.raises(ValidationError, match="attempt"):
        FixerRunResult(llm_output=output, final_labels=[])  # type: ignore[call-arg]
