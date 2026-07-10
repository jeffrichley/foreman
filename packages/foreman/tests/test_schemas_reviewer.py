"""Tests for the ReviewerOutput + Finding Pydantic schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foreman.schemas.reviewer import Finding, ReviewerOutput, ReviewerRunResult


def _minimal_finding_dict() -> dict[str, object]:
    return {
        "severity": "important",
        "target": "Acceptance criteria bullet 3",
        "issue": "Sub-request 2 uses 'improve' which is not testable.",
        "needed": "Replace with a concrete verb naming the operation.",
    }


def _minimal_clean_output_dict() -> dict[str, object]:
    return {
        "outcome": "clean",
        "review_comment": (
            "Clean. Traced ACs 1-4 against the spec and verified file paths in packages/foo/."
        ),
        "findings": [],
        "confidence": "high",
    }


# ----------------------------------------------------------------------
# Finding
# ----------------------------------------------------------------------


def test_finding_validates_valid_dict() -> None:
    f = Finding.model_validate(_minimal_finding_dict())
    assert f.severity == "important"
    assert f.target == "Acceptance criteria bullet 3"


def test_finding_rejects_bad_severity() -> None:
    bad = _minimal_finding_dict()
    bad["severity"] = "blocker"
    with pytest.raises(ValidationError, match="severity"):
        Finding.model_validate(bad)


def test_finding_rejects_missing_required_fields() -> None:
    bad = _minimal_finding_dict()
    del bad["issue"]
    with pytest.raises(ValidationError, match="issue"):
        Finding.model_validate(bad)


# ----------------------------------------------------------------------
# ReviewerOutput
# ----------------------------------------------------------------------


def test_reviewer_output_clean_with_no_findings() -> None:
    obj = ReviewerOutput.model_validate(_minimal_clean_output_dict())
    assert obj.outcome == "clean"
    assert obj.findings == []
    assert obj.confidence == "high"


def test_reviewer_output_needs_fix_with_findings() -> None:
    data = {
        "outcome": "needs_fix",
        "review_comment": "needs_fix — spec references a missing module.",
        "findings": [_minimal_finding_dict()],
    }
    obj = ReviewerOutput.model_validate(data)
    assert obj.outcome == "needs_fix"
    assert len(obj.findings) == 1
    # Default confidence is medium when omitted
    assert obj.confidence == "medium"


def test_reviewer_output_rejects_bad_outcome() -> None:
    bad = _minimal_clean_output_dict()
    bad["outcome"] = "approved"
    with pytest.raises(ValidationError, match="outcome"):
        ReviewerOutput.model_validate(bad)


def test_reviewer_output_rejects_bad_confidence() -> None:
    bad = _minimal_clean_output_dict()
    bad["confidence"] = "very-high"
    with pytest.raises(ValidationError, match="confidence"):
        ReviewerOutput.model_validate(bad)


def test_reviewer_output_rejects_minor_finding() -> None:
    """The model_validator enforces the prompt contract: minor
    observations belong in review_comment prose, not in structured
    findings.
    """
    data = {
        "outcome": "needs_fix",
        "review_comment": "needs_fix",
        "findings": [
            {**_minimal_finding_dict(), "severity": "minor"},
        ],
    }
    with pytest.raises(ValidationError, match="minor"):
        ReviewerOutput.model_validate(data)


def test_reviewer_output_rejects_clean_with_findings() -> None:
    """outcome=clean with non-empty findings is a self-contradiction
    the schema rejects — clean means no structured findings."""
    data = {
        "outcome": "clean",
        "review_comment": "clean",
        "findings": [_minimal_finding_dict()],
    }
    with pytest.raises(ValidationError, match="clean"):
        ReviewerOutput.model_validate(data)


def test_reviewer_output_rejects_needs_fix_with_empty_findings() -> None:
    """outcome=needs_fix with an empty findings list leaves the Fixer
    with nothing to act on — schema rejects."""
    data = {
        "outcome": "needs_fix",
        "review_comment": "needs_fix",
        "findings": [],
    }
    with pytest.raises(ValidationError, match="needs_fix"):
        ReviewerOutput.model_validate(data)


def test_reviewer_output_accepts_needs_fix_with_important_only() -> None:
    """A single important finding now blocks (changed from 'two+
    important' — any important now drives needs_fix)."""
    data = {
        "outcome": "needs_fix",
        "review_comment": "needs_fix — one important finding.",
        "findings": [_minimal_finding_dict()],  # severity="important"
    }
    obj = ReviewerOutput.model_validate(data)
    assert obj.outcome == "needs_fix"
    assert len(obj.findings) == 1
    assert obj.findings[0].severity == "important"


def test_reviewer_output_json_schema_is_serializable() -> None:
    schema = ReviewerOutput.model_json_schema()
    assert isinstance(schema, dict)
    assert "properties" in schema
    assert "outcome" in schema["properties"]
    assert "findings" in schema["properties"]
    assert "review_comment" in schema["properties"]


# ----------------------------------------------------------------------
# ReviewerRunResult (foreman#91 wrapper)
# ----------------------------------------------------------------------


def test_reviewer_run_result_carries_llm_output_and_final_labels() -> None:
    llm = ReviewerOutput.model_validate(_minimal_clean_output_dict())
    result = ReviewerRunResult(
        llm_output=llm,
        final_labels=["foreman:spec-ready"],
    )
    assert result.llm_output.outcome == "clean"
    assert result.final_labels == ["foreman:spec-ready"]


def test_reviewer_run_result_rejects_non_list_final_labels() -> None:
    """``final_labels`` must be a ``list[str]`` — a single string MUST
    raise (not be coerced to a 1-char list)."""
    llm = ReviewerOutput.model_validate(_minimal_clean_output_dict())
    with pytest.raises(ValidationError, match="final_labels"):
        ReviewerRunResult.model_validate(
            {"llm_output": llm.model_dump(), "final_labels": "foreman:spec-ready"}
        )


def test_reviewer_run_result_rejects_non_string_label_elements() -> None:
    llm = ReviewerOutput.model_validate(_minimal_clean_output_dict())
    with pytest.raises(ValidationError, match="final_labels"):
        ReviewerRunResult.model_validate({"llm_output": llm.model_dump(), "final_labels": [42]})


def test_reviewer_run_result_requires_final_labels() -> None:
    llm = ReviewerOutput.model_validate(_minimal_clean_output_dict())
    with pytest.raises(ValidationError, match="final_labels"):
        ReviewerRunResult.model_validate({"llm_output": llm.model_dump()})
