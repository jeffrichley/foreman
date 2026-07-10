"""Planner v4 CLI emits FOREMAN_OUTCOME(kind=TRANSIENT_PROVIDER_ERROR)
when ``_run_planner_for_v4`` raises ``ProviderTransientError``
(foreman#361).
"""

from __future__ import annotations

from unittest.mock import patch

from foreman.providers import ProviderTransientError
from foreman.roles.planner import run_planner_cli
from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout


def test_planner_transient_outcome(capsys) -> None:
    """A ``ProviderTransientError`` from the planner core emits
    ``TRANSIENT_PROVIDER_ERROR`` with details populated, AND exits 0
    (the outcome JSON carries the signal; non-zero would trip
    ``RoleSubprocessError`` in the dispatcher and lose the
    discriminator).
    """
    original_cause = Exception("Claude Code returned an error result: 503 Service Unavailable")
    transient = ProviderTransientError(str(original_cause))
    transient.__cause__ = original_cause
    with patch(
        "foreman.roles.planner._run_planner_for_v4",
        side_effect=transient,
    ):
        exit_code = run_planner_cli(project="p", issue_number=1)

    assert exit_code == 0
    captured = capsys.readouterr()
    outcome = parse_outcome_from_stdout(captured.out)
    assert outcome.kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR
    assert "503" in outcome.details["provider_status"]
    assert outcome.details["exception_class"] == "Exception"
