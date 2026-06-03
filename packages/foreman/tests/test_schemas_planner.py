"""Tests for PlannerOutput + PlannerRunResult Pydantic schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foreman.git_host import PRRef
from foreman.schemas.planner import PlannerOutput, PlannerRunResult


def _minimal_output_dict() -> dict[str, object]:
    return {
        "spec_doc_content": "# Spec\n\n## Goal\nDo a thing.\n",
        "pr_title": "spec: do a thing",
        "pr_body": "Adds the spec for doing a thing. Link: ...",
        "summary": "Drafted spec for the thing.",
        "considered_alternatives": ["smaller scope", "do nothing"],
        "confidence": "medium",
    }


def test_planner_output_validates_valid_dict() -> None:
    obj = PlannerOutput.model_validate(_minimal_output_dict())
    assert obj.pr_title == "spec: do a thing"
    assert obj.confidence == "medium"
    assert len(obj.considered_alternatives) == 2


def test_planner_output_defaults_for_optional_fields() -> None:
    minimal = {
        "spec_doc_content": "# Spec",
        "pr_title": "spec: x",
        "pr_body": "y",
        "summary": "z",
    }
    obj = PlannerOutput.model_validate(minimal)
    assert obj.confidence == "medium"
    assert obj.considered_alternatives == []


def test_planner_output_rejects_missing_required_field() -> None:
    bad = _minimal_output_dict()
    del bad["spec_doc_content"]
    with pytest.raises(ValidationError, match="spec_doc_content"):
        PlannerOutput.model_validate(bad)


def test_planner_output_rejects_bad_confidence_value() -> None:
    bad = _minimal_output_dict()
    bad["confidence"] = "extremely-confident-bro"
    with pytest.raises(ValidationError, match="confidence"):
        PlannerOutput.model_validate(bad)


def test_planner_output_json_schema_is_serializable() -> None:
    schema = PlannerOutput.model_json_schema()
    assert isinstance(schema, dict)
    assert "properties" in schema
    assert "spec_doc_content" in schema["properties"]
    assert "pr_title" in schema["properties"]
    # The LLM no longer returns PR-side details (URL/number) — those are
    # core's responsibility now.
    assert "pr_url" not in schema["properties"]
    assert "pr_number" not in schema["properties"]


def test_planner_run_result_carries_llm_output_and_pr() -> None:
    llm = PlannerOutput.model_validate(_minimal_output_dict())
    pr = PRRef(
        number=42,
        url="https://github.com/o/r/pull/42",
        title="spec: do a thing",
        body="x",
        branch="foreman/issue-1",
        base_branch="main",
        repo_slug="o/r",
    )
    result = PlannerRunResult(
        llm_output=llm,
        pr=pr,
        final_labels=["foreman:spec-review"],
    )
    assert result.pr.number == 42
    assert result.llm_output.confidence == "medium"
    assert result.final_labels == ["foreman:spec-review"]
