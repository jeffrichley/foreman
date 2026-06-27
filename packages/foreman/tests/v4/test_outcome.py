"""Tests for the Outcome model — the role-to-daemon reporting contract."""
import json

import pytest
from pydantic import ValidationError

from foreman.v4.outcome import (
    Finding,
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
)


def test_outcome_minimal_clean():
    outcome = Outcome(
        kind=OutcomeKind.CLEAN,
        confidence=OutcomeConfidence.HIGH,
        summary="spec PR opened",
    )
    assert outcome.schema_version == 1
    assert outcome.findings == []
    assert outcome.artifacts.pr_url is None


def test_outcome_needs_fix_with_findings():
    outcome = Outcome(
        kind=OutcomeKind.NEEDS_FIX,
        confidence=OutcomeConfidence.HIGH,
        summary="reviewer found 2 issues",
        findings=[
            Finding(severity="critical", location="foo.py:42", description="null deref"),
            Finding(severity="minor", location="general", description="naming nit"),
        ],
    )
    assert len(outcome.findings) == 2
    assert outcome.findings[0].severity == "critical"


def test_outcome_artifacts_pr():
    outcome = Outcome(
        kind=OutcomeKind.CLEAN,
        confidence=OutcomeConfidence.HIGH,
        summary="impl PR open",
        artifacts=OutcomeArtifacts(
            pr_url="https://github.com/x/y/pull/1",
            pr_number=1,
            commit_sha="abc123",
            branch="impl/1",
        ),
    )
    assert outcome.artifacts.pr_number == 1


def test_outcome_summary_max_length():
    with pytest.raises(ValidationError):
        Outcome(
            kind=OutcomeKind.CLEAN,
            confidence=OutcomeConfidence.HIGH,
            summary="x" * 501,
        )


def test_outcome_round_trips_through_json():
    original = Outcome(
        kind=OutcomeKind.BLOCKED,
        confidence=OutcomeConfidence.MEDIUM,
        summary="CI in flight",
        artifacts=OutcomeArtifacts(pr_number=42),
    )
    raw = original.model_dump_json()
    reloaded = Outcome.model_validate_json(raw)
    assert reloaded == original


def test_outcome_finding_severity_rejects_unknown():
    with pytest.raises(ValidationError):
        Finding(severity="catastrophic", location="x", description="y")


def test_outcome_kind_enum_values():
    assert OutcomeKind.CLEAN.value == "clean"
    assert OutcomeKind.NEEDS_FIX.value == "needs_fix"
    assert OutcomeKind.BLOCKED.value == "blocked"
    assert OutcomeKind.NEEDS_HELP.value == "needs_help"
    assert OutcomeKind.ERROR.value == "error"


def test_outcome_default_schema_version_is_1():
    raw = '{"kind":"clean","confidence":"high","summary":"x"}'
    outcome = Outcome.model_validate(json.loads(raw))
    assert outcome.schema_version == 1


def test_outcome_details_defaults_to_empty_dict():
    """V0 contract (foreman#315): details is freeform and defaults to {}.

    Backward compatibility — every existing call site that constructs
    an Outcome without ``details=`` continues to work, and the
    serialized JSON carries an empty dict that consumers can ignore.
    """
    outcome = Outcome(
        kind=OutcomeKind.CLEAN,
        confidence=OutcomeConfidence.HIGH,
        summary="x",
    )
    assert outcome.details == {}


def test_outcome_details_field_serializes_round_trip():
    """details survives model_dump_json → model_validate_json.

    The state machine pickles Outcome through SQLite's
    state_instances.outcome_payload column via outcome.model_dump(mode='json')
    plus json.dumps, and the EventBus consumers pull it back via
    Outcome.model_validate_json. If details didn't round-trip we'd lose
    the diagnostic detail at the SQLite boundary even if the role
    emitted it correctly.
    """
    original = Outcome(
        kind=OutcomeKind.NEEDS_HELP,
        confidence=OutcomeConfidence.HIGH,
        summary="incomplete (attempt 1)",
        details={
            "work_comment": "could not finish — algokit Justfile has no `check` recipe",
            "did_check_pass": False,
            "check_output_summary": "just: error: Justfile has no recipe named `check`",
            "confidence": "low",
        },
    )
    raw = original.model_dump_json()
    reloaded = Outcome.model_validate_json(raw)
    assert reloaded == original
    assert reloaded.details["did_check_pass"] is False
    assert "no `check` recipe" in reloaded.details["work_comment"]


# --- is_runaway_exempt ---


from foreman.v4.outcome import is_runaway_exempt, is_transient_error_exempt  # noqa: E402


class TestIsRunawayExempt:
    """Unit tests for the runaway-cap exemption predicate (issue #455)."""

    def test_exempt_when_failure_phase_is_can_run(self):
        assert is_runaway_exempt("can_run", None) is True

    def test_exempt_when_failure_phase_is_crash_recovery(self):
        assert is_runaway_exempt("crash_recovery", None) is True

    def test_exempt_when_outcome_kind_is_blocked(self):
        assert is_runaway_exempt(None, OutcomeKind.BLOCKED) is True

    def test_exempt_when_outcome_kind_is_transient_provider_error(self):
        assert is_runaway_exempt(None, OutcomeKind.TRANSIENT_PROVIDER_ERROR) is True

    def test_not_exempt_for_clean_outcome(self):
        assert is_runaway_exempt(None, OutcomeKind.CLEAN) is False

    def test_not_exempt_for_none_phase_and_none_kind(self):
        assert is_runaway_exempt(None, None) is False

    def test_not_exempt_for_arbitrary_failure_phase(self):
        assert is_runaway_exempt("execute", None) is False


class TestIsTransientErrorExempt:
    """Unit tests for the transient-provider-error counter exemption predicate (issue #455)."""

    def test_exempt_when_failure_phase_is_can_run(self):
        assert is_transient_error_exempt("can_run", None) is True

    def test_exempt_when_outcome_kind_is_none(self):
        # In-flight row (outcome_kind not yet written).
        assert is_transient_error_exempt(None, None) is True

    def test_not_exempt_for_transient_provider_error_outcome(self):
        # TRANSIENT_PROVIDER_ERROR is counted, not skipped.
        assert is_transient_error_exempt(None, OutcomeKind.TRANSIENT_PROVIDER_ERROR) is False

    def test_not_exempt_for_clean_outcome(self):
        assert is_transient_error_exempt(None, OutcomeKind.CLEAN) is False

    def test_not_exempt_for_arbitrary_failure_phase(self):
        assert is_transient_error_exempt("execute", OutcomeKind.CLEAN) is False
