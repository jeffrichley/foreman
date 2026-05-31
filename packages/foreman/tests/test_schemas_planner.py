"""Tests for PlannerOutput Pydantic schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foreman.schemas.planner import PlannerOutput


def test_planner_output_validates_valid_dict() -> None:
    obj = PlannerOutput.model_validate(
        {
            "pr_url": "https://github.com/jeffrichley/voice/pull/42",
            "pr_number": 42,
            "branch_name": "foreman/issue-7",
            "summary": "Drafted spec for SSML support in madrigal.",
            "considered_alternatives": ["raw-string approach", "external library"],
            "confidence": "medium",
        }
    )
    assert obj.pr_number == 42
    assert obj.confidence == "medium"
    assert len(obj.considered_alternatives) == 2


def test_planner_output_rejects_missing_required_field() -> None:
    with pytest.raises(ValidationError, match="pr_url"):
        PlannerOutput.model_validate(
            {
                "pr_number": 42,
                "branch_name": "foreman/issue-7",
                "summary": "x",
            }
        )


def test_planner_output_rejects_bad_confidence_value() -> None:
    with pytest.raises(ValidationError, match="confidence"):
        PlannerOutput.model_validate(
            {
                "pr_url": "https://github.com/jeffrichley/voice/pull/42",
                "pr_number": 42,
                "branch_name": "foreman/issue-7",
                "summary": "x",
                "confidence": "extremely-confident-bro",
            }
        )


def test_planner_output_json_schema_is_serializable() -> None:
    schema = PlannerOutput.model_json_schema()
    assert isinstance(schema, dict)
    assert "properties" in schema
    assert "pr_url" in schema["properties"]
