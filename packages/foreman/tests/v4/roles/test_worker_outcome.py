"""Worker v4 CLI emits FOREMAN_OUTCOME on exit.

The Worker is the only role with FOUR outcome paths:

- ``CLEAN`` — impl PR opened, CI green (``status="ci_passing"``).
- ``BLOCKED`` — impl PR opened, CI still in flight
  (``status="ci_in_flight"``). The v4 state machine
  (``ImplementingState.next_state``) handles BLOCKED by re-polling on
  the next tick. Worker is the ONLY role producing this kind.
- ``NEEDS_HELP`` — Worker hit a give-up condition
  (``status="give_up"``, e.g., 3 baseline-failure attempts). Routes to
  NeedsHelpState (operator-resumable).
- ``ERROR`` — top-level role boundary exception.

Worker is NOT target-aware (impl PR is unambiguous from issue
number), so no ``target`` kwarg.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from foreman.roles.worker import run_worker_cli
from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout


def _result(*, status: str, pr_number: int | None = None, summary: str = "x"):
    """v4-shaped result the helper ``_run_worker_for_v4`` returns.

    ``run_worker_cli`` consumes ``status`` / ``pr_number`` / ``summary``
    via ``getattr``, so a MagicMock with the right attribute names is
    enough — no real Pydantic instance required.
    """
    r = MagicMock()
    r.status = status
    r.pr_number = pr_number
    r.summary = summary
    return r


def test_ci_passing_emits_clean(capsys):
    """Impl PR open, CI green → CLEAN with pr_number on artifacts."""
    fake = _result(status="ci_passing", pr_number=99, summary="impl PR open, CI green")
    with patch(
        "foreman.roles.worker._run_worker_for_v4",
        return_value=fake,
    ):
        exit_code = run_worker_cli(project="p", issue_number=1)
    assert exit_code == 0
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.CLEAN
    assert outcome.artifacts.pr_number == 99


def test_ci_in_flight_emits_blocked(capsys):
    """Impl PR open, CI still running → BLOCKED with pr_number on artifacts.

    Worker is the ONLY role emitting BLOCKED. The v4 state machine
    re-dispatches the Worker on the next tick via
    ``ImplementingState.next_state`` returning a fresh
    ``ImplementingState()``.
    """
    fake = _result(status="ci_in_flight", pr_number=99, summary="impl PR open, CI in flight")
    with patch(
        "foreman.roles.worker._run_worker_for_v4",
        return_value=fake,
    ):
        exit_code = run_worker_cli(project="p", issue_number=1)
    assert exit_code == 0  # zero exit — BLOCKED is a re-poll signal, not a failure
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.BLOCKED
    assert outcome.artifacts.pr_number == 99


def test_give_up_emits_needs_help(capsys):
    """Worker hit give-up condition (e.g., 3 baseline failures) → NEEDS_HELP."""
    fake = _result(status="give_up", summary="3 baseline failures")
    with patch(
        "foreman.roles.worker._run_worker_for_v4",
        return_value=fake,
    ):
        exit_code = run_worker_cli(project="p", issue_number=1)
    assert exit_code == 0  # zero exit — stdout carries the verdict
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.NEEDS_HELP


def test_worker_exception_emits_error(capsys):
    """Worker raised at the role boundary → ERROR + exit 1."""
    with patch(
        "foreman.roles.worker._run_worker_for_v4",
        side_effect=RuntimeError("worktree corrupted"),
    ):
        exit_code = run_worker_cli(project="p", issue_number=1)
    assert exit_code == 1
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.ERROR
    assert "worktree corrupted" in outcome.summary
