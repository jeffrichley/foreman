"""Tests for FOREMAN_OUTCOME: marker parsing.

The role's stdout has a human-readable trace followed by a single terminal
line beginning with FOREMAN_OUTCOME: and ending with the JSON outcome. The
parser scans stdout in reverse for the marker. Three distinct failures:
missing marker, malformed JSON, schema-invalid JSON.
"""
import pytest

from foreman.v4.outcome import (
    OutcomeInvalidError,
    OutcomeKind,
    OutcomeMalformedError,
    OutcomeMissingError,
    parse_outcome_from_stdout,
)


def test_parses_outcome_from_terminal_line():
    stdout = (
        "doing things\n"
        "more things\n"
        'FOREMAN_OUTCOME:{"schema_version":1,"kind":"clean","confidence":"high","summary":"ok"}\n'
    )
    outcome = parse_outcome_from_stdout(stdout)
    assert outcome.kind == OutcomeKind.CLEAN


def test_parses_when_marker_is_last_line_without_trailing_newline():
    stdout = 'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}'
    outcome = parse_outcome_from_stdout(stdout)
    assert outcome.summary == "ok"


def test_ignores_earlier_lines_that_look_like_json():
    stdout = (
        '{"kind":"error","confidence":"low","summary":"this is a log line"}\n'
        'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}\n'
    )
    outcome = parse_outcome_from_stdout(stdout)
    assert outcome.kind == OutcomeKind.CLEAN


def test_uses_last_marker_when_multiple_present():
    stdout = (
        'FOREMAN_OUTCOME:{"kind":"error","confidence":"low","summary":"early"}\n'
        'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"final"}\n'
    )
    outcome = parse_outcome_from_stdout(stdout)
    assert outcome.summary == "final"


def test_missing_marker_raises_outcome_missing():
    with pytest.raises(OutcomeMissingError):
        parse_outcome_from_stdout("just some log output\nno marker here\n")


def test_malformed_json_raises_outcome_malformed():
    stdout = "FOREMAN_OUTCOME:{not valid json}\n"
    with pytest.raises(OutcomeMalformedError) as exc:
        parse_outcome_from_stdout(stdout)
    assert "{not valid json}" in str(exc.value)


def test_schema_invalid_raises_outcome_invalid():
    stdout = 'FOREMAN_OUTCOME:{"kind":"catastrophic","confidence":"high","summary":"x"}\n'
    with pytest.raises(OutcomeInvalidError):
        parse_outcome_from_stdout(stdout)


def test_empty_stdout_raises_outcome_missing():
    with pytest.raises(OutcomeMissingError):
        parse_outcome_from_stdout("")
