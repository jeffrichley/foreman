"""Tests for the WorkerOutput + WorkerRunResult Pydantic schemas.

Coverage:
- ImplementedSubRequest / SkippedSubRequest / CommitMade — happy + invalid
- WorkerOutput — happy paths for all three outcomes
- WorkerOutput — required-iff-outcome validation (the load-bearing
  invariant Foreman core relies on for PR-open vs not-open dispatch)
- WorkerRunResult — bundling + final_did_check_pass discipline
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foreman.schemas.worker import (
    CommitMade,
    ImplementedSubRequest,
    SkippedSubRequest,
    WorkerOutput,
    WorkerRunResult,
)

# ----------------------------------------------------------------------
# Fixture helpers
# ----------------------------------------------------------------------


def _implemented_sub_request() -> dict[str, object]:
    return {
        "spec_reference": "Sub-request 1",
        "summary": "Added X class with method Y.",
        "files_touched": ["packages/foo/src/foo/x.py"],
        "tests_added": ["test_x_returns_y"],
    }


def _skipped_sub_request(reason: str = "needs_info") -> dict[str, object]:
    return {
        "spec_reference": "Sub-request 4",
        "reason": reason,
        "rationale": (
            "The spec asks for a specific throughput target but the issue "
            "does not name a concrete number; cannot implement without "
            "inventing facts."
        ),
    }


def _commit() -> dict[str, object]:
    return {
        "sha": "a" * 40,
        "summary": "feat(foo): add X class",
        "files_changed": ["packages/foo/src/foo/x.py", "packages/foo/tests/test_x.py"],
    }


# ----------------------------------------------------------------------
# ImplementedSubRequest
# ----------------------------------------------------------------------


def test_implemented_sub_request_validates_valid_dict() -> None:
    sr = ImplementedSubRequest.model_validate(_implemented_sub_request())
    assert sr.spec_reference == "Sub-request 1"
    assert sr.files_touched == ["packages/foo/src/foo/x.py"]


def test_implemented_sub_request_files_touched_default_empty() -> None:
    """``files_touched`` defaults to empty — valid for spec-doc-only changes."""
    sr = ImplementedSubRequest.model_validate({"spec_reference": "AC 1", "summary": "x"})
    assert sr.files_touched == []
    assert sr.tests_added == []


def test_implemented_sub_request_requires_spec_reference_and_summary() -> None:
    bad = _implemented_sub_request()
    del bad["summary"]
    with pytest.raises(ValidationError, match="summary"):
        ImplementedSubRequest.model_validate(bad)


# ----------------------------------------------------------------------
# SkippedSubRequest
# ----------------------------------------------------------------------


def test_skipped_sub_request_validates_with_needs_info() -> None:
    sr = SkippedSubRequest.model_validate(_skipped_sub_request("needs_info"))
    assert sr.reason == "needs_info"


def test_skipped_sub_request_validates_with_spec_unclear() -> None:
    sr = SkippedSubRequest.model_validate(_skipped_sub_request("spec_unclear"))
    assert sr.reason == "spec_unclear"


def test_skipped_sub_request_validates_with_out_of_scope() -> None:
    sr = SkippedSubRequest.model_validate(_skipped_sub_request("out_of_scope"))
    assert sr.reason == "out_of_scope"


def test_skipped_sub_request_validates_with_spec_invalid_partial() -> None:
    sr = SkippedSubRequest.model_validate(_skipped_sub_request("spec_invalid_partial"))
    assert sr.reason == "spec_invalid_partial"


def test_skipped_sub_request_rejects_unknown_reason() -> None:
    bad = _skipped_sub_request()
    bad["reason"] = "deferred-to-human"
    with pytest.raises(ValidationError, match="reason"):
        SkippedSubRequest.model_validate(bad)


def test_skipped_sub_request_requires_rationale() -> None:
    bad = _skipped_sub_request()
    del bad["rationale"]
    with pytest.raises(ValidationError, match="rationale"):
        SkippedSubRequest.model_validate(bad)


# ----------------------------------------------------------------------
# CommitMade
# ----------------------------------------------------------------------


def test_commit_made_validates_valid_dict() -> None:
    c = CommitMade.model_validate(_commit())
    assert c.sha == "a" * 40
    assert len(c.files_changed) == 2


def test_commit_made_files_changed_default_empty() -> None:
    c = CommitMade.model_validate({"sha": "b" * 40, "summary": "x"})
    assert c.files_changed == []


# ----------------------------------------------------------------------
# WorkerOutput — happy paths
# ----------------------------------------------------------------------


def _minimal_implemented() -> dict[str, object]:
    return {
        "outcome": "implemented",
        "work_comment": "implemented all sub-requests; just check passed.",
        "pr_title": "feat(foo): add X class",
        "pr_body": "Implements #42.\n\n## What was implemented\n- X class.\n",
        "commits_made": [_commit()],
        "implemented_sub_requests": [_implemented_sub_request()],
        "skipped_sub_requests": [],
        "did_check_pass": True,
        "confidence": "high",
    }


def _escalation_payload() -> dict[str, object]:
    """Minimal escalation_comment payload satisfying foreman#367 validator."""
    return {
        "why": "Worker could not finish for reason X.",
        "what_tried": "Read the spec, walked the sub-requests.",
        "what_would_unblock": "Operator clarifies the conflicting sub-request.",
    }


def _minimal_incomplete() -> dict[str, object]:
    return {
        "outcome": "incomplete",
        "work_comment": "incomplete — check_command failed on test_x.",
        "commits_made": [_commit()],
        "implemented_sub_requests": [],
        "skipped_sub_requests": [_skipped_sub_request()],
        "did_check_pass": False,
        "check_output_summary": "test_x_returns_y failed: assertion mismatch on row 3.",
        # foreman#367: required-iff outcome in {'incomplete',
        # 'spec_invalid'}; the validator rejects None.
        "escalation_comment": _escalation_payload(),
    }


def _minimal_spec_invalid() -> dict[str, object]:
    return {
        "outcome": "spec_invalid",
        "work_comment": "spec_invalid — issue and spec contradict.",
        "spec_invalid_reason": (
            "Issue line 7 says 'must use SSML' but the spec's sub-request 2 "
            "says 'plain text only'. The contradiction is unresolvable without "
            "human input."
        ),
        "commits_made": [],
        "implemented_sub_requests": [],
        "skipped_sub_requests": [],
        "did_check_pass": True,
        # foreman#367: required-iff outcome in {'incomplete',
        # 'spec_invalid'}.
        "escalation_comment": _escalation_payload(),
    }


def test_worker_output_implemented_round_trips() -> None:
    obj = WorkerOutput.model_validate(_minimal_implemented())
    assert obj.outcome == "implemented"
    assert obj.pr_title == "feat(foo): add X class"
    assert obj.did_check_pass is True


def test_worker_output_incomplete_round_trips() -> None:
    obj = WorkerOutput.model_validate(_minimal_incomplete())
    assert obj.outcome == "incomplete"
    assert obj.confidence == "medium"  # default
    assert obj.pr_title is None
    assert obj.pr_body is None
    assert obj.spec_invalid_reason is None


def test_worker_output_spec_invalid_round_trips() -> None:
    obj = WorkerOutput.model_validate(_minimal_spec_invalid())
    assert obj.outcome == "spec_invalid"
    assert obj.spec_invalid_reason is not None
    assert "contradict" in obj.spec_invalid_reason


# ----------------------------------------------------------------------
# WorkerOutput — Literal field validation
# ----------------------------------------------------------------------


def test_worker_output_rejects_bad_outcome() -> None:
    bad = _minimal_implemented()
    bad["outcome"] = "partial"
    with pytest.raises(ValidationError, match="outcome"):
        WorkerOutput.model_validate(bad)


def test_worker_output_rejects_bad_confidence() -> None:
    bad = _minimal_implemented()
    bad["confidence"] = "very-high"
    with pytest.raises(ValidationError, match="confidence"):
        WorkerOutput.model_validate(bad)


def test_worker_output_did_check_pass_is_mandatory() -> None:
    """The honesty gate is required — no default, no opt-out."""
    bad = _minimal_implemented()
    del bad["did_check_pass"]
    with pytest.raises(ValidationError, match="did_check_pass"):
        WorkerOutput.model_validate(bad)


# ----------------------------------------------------------------------
# WorkerOutput — required-iff-outcome validation
# ----------------------------------------------------------------------


def test_implemented_without_pr_title_raises() -> None:
    bad = _minimal_implemented()
    bad["pr_title"] = None
    with pytest.raises(ValidationError, match="pr_title"):
        WorkerOutput.model_validate(bad)


def test_implemented_without_pr_body_raises() -> None:
    bad = _minimal_implemented()
    bad["pr_body"] = None
    with pytest.raises(ValidationError, match="pr_body"):
        WorkerOutput.model_validate(bad)


def test_implemented_with_spec_invalid_reason_raises() -> None:
    """An ``implemented`` outcome with ``spec_invalid_reason`` set is a
    contradiction — the work landed, so why is the spec invalid?
    """
    bad = _minimal_implemented()
    bad["spec_invalid_reason"] = "contradictory; should not be present"
    with pytest.raises(ValidationError, match="spec_invalid_reason"):
        WorkerOutput.model_validate(bad)


def test_spec_invalid_without_reason_raises() -> None:
    bad = _minimal_spec_invalid()
    bad["spec_invalid_reason"] = None
    with pytest.raises(ValidationError, match="spec_invalid_reason"):
        WorkerOutput.model_validate(bad)


def test_spec_invalid_with_pr_title_raises() -> None:
    bad = _minimal_spec_invalid()
    bad["pr_title"] = "feat: should not have a title"
    with pytest.raises(ValidationError, match="pr_title|pr_body"):
        WorkerOutput.model_validate(bad)


def test_spec_invalid_with_pr_body_raises() -> None:
    bad = _minimal_spec_invalid()
    bad["pr_body"] = "should not be present"
    with pytest.raises(ValidationError, match="pr_title|pr_body"):
        WorkerOutput.model_validate(bad)


def test_incomplete_with_pr_title_raises() -> None:
    bad = _minimal_incomplete()
    bad["pr_title"] = "feat: nope"
    with pytest.raises(ValidationError, match="pr_title|pr_body|spec_invalid_reason"):
        WorkerOutput.model_validate(bad)


def test_incomplete_with_pr_body_raises() -> None:
    bad = _minimal_incomplete()
    bad["pr_body"] = "nope"
    with pytest.raises(ValidationError, match="pr_title|pr_body|spec_invalid_reason"):
        WorkerOutput.model_validate(bad)


def test_incomplete_with_spec_invalid_reason_raises() -> None:
    bad = _minimal_incomplete()
    bad["spec_invalid_reason"] = "nope"
    with pytest.raises(ValidationError, match="pr_title|pr_body|spec_invalid_reason"):
        WorkerOutput.model_validate(bad)


# ----------------------------------------------------------------------
# WorkerOutput — schema is JSON-serializable for the SDK
# ----------------------------------------------------------------------


def test_worker_output_json_schema_is_serializable() -> None:
    schema = WorkerOutput.model_json_schema()
    assert isinstance(schema, dict)
    assert "properties" in schema
    assert "outcome" in schema["properties"]
    assert "did_check_pass" in schema["properties"]
    assert "pr_title" in schema["properties"]
    assert "spec_invalid_reason" in schema["properties"]


# ----------------------------------------------------------------------
# WorkerRunResult
# ----------------------------------------------------------------------


def test_worker_run_result_bundles_output_and_orchestrator_state() -> None:
    output = WorkerOutput.model_validate(_minimal_implemented())
    result = WorkerRunResult(
        llm_output=output,
        attempt=1,
        pr_url="https://github.com/owner/repo/pull/101",
        final_did_check_pass=True,
        final_labels=["foreman:impl-review"],
    )
    assert result.attempt == 1
    assert result.pr_url == "https://github.com/owner/repo/pull/101"
    assert result.final_did_check_pass is True
    assert result.final_labels == ["foreman:impl-review"]


def test_worker_run_result_requires_attempt() -> None:
    output = WorkerOutput.model_validate(_minimal_implemented())
    with pytest.raises(ValidationError, match="attempt"):
        WorkerRunResult(  # type: ignore[call-arg]
            llm_output=output,
            final_did_check_pass=True,
            final_labels=[],
        )


def test_worker_run_result_pr_url_defaults_to_none_for_incomplete() -> None:
    """Incomplete/spec_invalid runs don't open a PR — pr_url is None."""
    output = WorkerOutput.model_validate(_minimal_incomplete())
    result = WorkerRunResult(
        llm_output=output,
        attempt=1,
        final_did_check_pass=False,
        final_labels=["foreman:implementing", "foreman:needs-help"],
    )
    assert result.pr_url is None


def test_worker_run_result_final_did_check_pass_is_mandatory() -> None:
    """The orchestrator-verified ground-truth field is required."""
    output = WorkerOutput.model_validate(_minimal_implemented())
    with pytest.raises(ValidationError, match="final_did_check_pass"):
        WorkerRunResult(  # type: ignore[call-arg]
            llm_output=output,
            attempt=1,
            final_labels=[],
        )
