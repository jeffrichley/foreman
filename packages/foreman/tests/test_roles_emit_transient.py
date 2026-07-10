"""Direct tests for ``foreman.roles.emit_transient_provider_outcome``
(foreman#361). The four per-role transient-outcome tests at
``tests/v4/roles/test_*_transient_outcome.py`` exercise the helper
indirectly through each role's CLI; these tests pin the helper's own
contract: exit code, outcome shape, and the ``exception_class`` walk
through ``exc.__cause__``.
"""

from __future__ import annotations

from foreman.providers import ProviderTransientError
from foreman.roles import emit_transient_provider_outcome
from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout


def test_emit_transient_provider_outcome_with_cause(capsys) -> None:
    """``exception_class`` reports the underlying SDK exception type
    (e.g. ``RateLimitError``) rather than ``ProviderTransientError``
    itself — operators reading the structured log learn what actually
    went wrong."""

    class _FakeRateLimitError(Exception):
        pass

    original = _FakeRateLimitError("Claude Code returned an error result: 429 Too Many Requests")
    transient = ProviderTransientError(str(original))
    transient.__cause__ = original

    exit_code = emit_transient_provider_outcome(transient)

    assert exit_code == 0
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR
    assert outcome.confidence.value == "high"
    assert "429" in outcome.details["provider_status"]
    assert outcome.details["exception_class"] == "_FakeRateLimitError"


def test_emit_transient_provider_outcome_without_cause(capsys) -> None:
    """When ``__cause__`` is None, ``exception_class`` falls back to the
    ``ProviderTransientError`` type name. Guards against an ``AttributeError``
    on the cause-walk and pins the documented fallback."""
    transient = ProviderTransientError("connection refused")

    exit_code = emit_transient_provider_outcome(transient)

    assert exit_code == 0
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR
    assert outcome.details["exception_class"] == "ProviderTransientError"
    assert outcome.details["provider_status"] == "connection refused"


def test_emit_transient_provider_outcome_summary_truncates(capsys) -> None:
    """A pathological multi-kilobyte provider message must not overflow
    the outcome summary — :func:`emit_transient_provider_outcome` slices
    to 500 chars (same shape the four role CLIs used before the dedupe;
    pinned here so a future inline edit cannot quietly drop the bound)."""
    long_msg = "X" * 5000
    transient = ProviderTransientError(long_msg)

    exit_code = emit_transient_provider_outcome(transient)

    assert exit_code == 0
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert len(outcome.summary) <= 500
    # The full string still survives in details for log forensics.
    assert outcome.details["provider_status"] == long_msg
