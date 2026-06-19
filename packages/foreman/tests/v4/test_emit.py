"""emit_outcome — writes the FOREMAN_OUTCOME: terminal line."""
from __future__ import annotations

import json
from io import StringIO

from foreman.v4.emit import emit_outcome
from foreman.v4.outcome import (
    Outcome,
    OutcomeArtifacts,
    OutcomeConfidence,
    OutcomeKind,
    parse_outcome_from_stdout,
)


def test_emit_writes_marker_with_json_payload():
    buf = StringIO()
    outcome = Outcome(
        kind=OutcomeKind.CLEAN,
        confidence=OutcomeConfidence.HIGH,
        summary="spec PR open",
        artifacts=OutcomeArtifacts(pr_number=42),
    )
    emit_outcome(outcome, stream=buf)
    line = buf.getvalue().strip()
    assert line.startswith("FOREMAN_OUTCOME:")
    payload = json.loads(line[len("FOREMAN_OUTCOME:"):])
    assert payload["kind"] == "clean"
    assert payload["artifacts"]["pr_number"] == 42


def test_emitted_line_round_trips_through_parser():
    buf = StringIO()
    original = Outcome(
        kind=OutcomeKind.NEEDS_FIX,
        confidence=OutcomeConfidence.MEDIUM,
        summary="reviewer found issues",
    )
    emit_outcome(original, stream=buf)
    parsed = parse_outcome_from_stdout(buf.getvalue())
    assert parsed == original


def test_emit_ends_with_newline():
    buf = StringIO()
    emit_outcome(
        Outcome(
            kind=OutcomeKind.CLEAN, confidence=OutcomeConfidence.HIGH,
            summary="x",
        ),
        stream=buf,
    )
    assert buf.getvalue().endswith("\n")
