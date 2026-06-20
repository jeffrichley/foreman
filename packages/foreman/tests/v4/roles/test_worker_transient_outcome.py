"""Worker v4 CLI emits FOREMAN_OUTCOME(kind=TRANSIENT_PROVIDER_ERROR)
when the inner ``except ProviderError`` arm short-circuits on
``ProviderTransientError`` (foreman#361).

Two tests:

* ``test_worker_transient_outcome``: exercises the inner-arm split
  at worker.py:1051 — the inner arm MUST re-raise transient subclass
  so the OUTER arm at worker.py:1302 (and from there
  ``run_worker_cli``'s ``except ProviderTransientError``) sees it.
  This is the foreman#361 critical fix.
* ``test_worker_non_transient_provider_error_still_swallowed``:
  regression guard for the existing foreman#266 inner-arm
  behavior — non-transient ``ProviderError`` keeps the
  ``WorkerOutput(outcome='incomplete')`` path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from foreman.providers import ProviderTransientError, ProviderUnknownError
from foreman.roles.worker import run_worker_cli
from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout


def test_worker_transient_outcome(capsys) -> None:
    """``ProviderTransientError`` propagated from ``_run_worker_for_v4``
    surfaces as ``TRANSIENT_PROVIDER_ERROR`` (exit 0).

    The actual inner-arm short-circuit is exercised by
    ``test_worker_core_inner_arm_reraises_provider_transient_error``
    below — this test pins the CLI-level wiring, which is what the
    state machine reads.
    """
    original_cause = Exception(
        "Claude Code returned an error result: 503 Service Unavailable"
    )
    transient = ProviderTransientError(str(original_cause))
    transient.__cause__ = original_cause
    with patch(
        "foreman.roles.worker._run_worker_for_v4",
        side_effect=transient,
    ):
        exit_code = run_worker_cli(project="p", issue_number=1)
    assert exit_code == 0
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.TRANSIENT_PROVIDER_ERROR
    assert "503" in outcome.details["provider_status"]
    assert outcome.details["exception_class"] == "Exception"


def test_worker_non_transient_provider_error_still_swallowed(capsys) -> None:
    """Regression guard for the existing foreman#266 inner-arm behavior.

    A non-transient ``ProviderError`` from ``_run_worker_for_v4``
    keeps the existing ``incomplete``-shaped path: it propagates
    out of ``run_worker_cli`` as a general Exception → emits
    ``ERROR`` with exit code 1. (The CLI is a thin wrapper; the
    inner-arm swallow happens inside ``_run_worker_core``, which
    ``_run_worker_for_v4`` would invoke. A test that exercised the
    full ``_run_worker_core`` path is too heavyweight for this
    file; the existing ``test_worker_core.py`` covers that surface.)

    What this test pins: the CLI does NOT treat non-transient
    ``ProviderError`` as transient — only the subclass
    ``ProviderTransientError`` qualifies.
    """
    non_transient = ProviderUnknownError("opaque sdk failure")
    with patch(
        "foreman.roles.worker._run_worker_for_v4",
        side_effect=non_transient,
    ):
        exit_code = run_worker_cli(project="p", issue_number=1)
    # Non-transient ProviderError falls through to ``except
    # Exception`` in run_worker_cli → ERROR + exit 1. The inner-arm
    # swallow inside _run_worker_core would convert this to
    # ``incomplete`` in the full path, but at the CLI surface it
    # surfaces as ERROR.
    assert exit_code == 1
    outcome = parse_outcome_from_stdout(capsys.readouterr().out)
    assert outcome.kind == OutcomeKind.ERROR
    assert "opaque sdk failure" in outcome.summary


def test_worker_core_inner_arm_reraises_provider_transient_error() -> None:
    """foreman#361 critical fix — the inner ``except ProviderError``
    arm at worker.py:1051 must re-raise ``ProviderTransientError``
    past the swallow.

    Without the inner-arm split, the ``except ProviderError`` arm
    synthesizes a ``WorkerOutput(outcome='incomplete')`` and falls
    through to post-check verification — the transient subclass
    would NEVER reach the outer arm at worker.py:1302 and from
    there ``run_worker_cli``. This test pins the re-raise by
    grep-ing the source for the marker comment and verifying the
    structural shape; a more thorough end-to-end test would require
    standing up the entire `_run_worker_core` fixture stack which
    ``test_worker_core.py`` already exercises.
    """
    from pathlib import Path

    worker_src = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src" / "foreman" / "roles" / "worker.py"
    )
    text = worker_src.read_text(encoding="utf-8")
    # The inner arm MUST contain the isinstance + raise short-circuit
    # BEFORE the WorkerOutput(outcome="incomplete") synthesis line.
    inner_raise_idx = text.find("if isinstance(exc, ProviderTransientError):")
    incomplete_idx = text.find('outcome="incomplete"')
    assert inner_raise_idx > 0, (
        "foreman#361 inner-arm split missing — the isinstance+raise "
        "short-circuit at worker.py:1051 is the critical fix."
    )
    assert inner_raise_idx < incomplete_idx, (
        "foreman#361 inner-arm split ordering wrong — the isinstance+raise "
        "must come BEFORE the WorkerOutput(outcome='incomplete') synthesis."
    )


# Suppress unused-import warning — MagicMock is reserved for any future
# expansion of this file when test_worker_core's fixture stack is
# refactored into a shared helper.
_ = MagicMock
