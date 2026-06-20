"""Phase 5 e2e — SubprocessRoleDispatcher actually forks and we read its stdout."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from foreman.v4.outcome import OutcomeKind, parse_outcome_from_stdout
from foreman.v4.subprocess_dispatcher import SubprocessRoleDispatcher


@pytest.fixture()
def stub_foreman(tmp_path: Path) -> Path:
    """Build a tiny script that mimics ``foreman`` for one canned response."""
    script = tmp_path / "stub_foreman.py"
    script.write_text(
        "import sys\n"
        "print('log line from stub')\n"
        "print('FOREMAN_OUTCOME:{\"kind\":\"clean\",\"confidence\":\"high\","
        "\"summary\":\"stub ok\"}')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    return script


def test_subprocess_round_trip(stub_foreman: Path, tmp_path: Path):
    identity = MagicMock()
    identity.get_role_token.return_value = "ghp_STUB"
    # foreman_cli points at python + stub script; CLI args after are ignored
    # by the stub but exercise the dispatcher's command-line construction.
    dispatcher = SubprocessRoleDispatcher(
        foreman_cli=[sys.executable, str(stub_foreman)],
        identity=identity,
        log_dir=tmp_path,
    )
    stdout = dispatcher.dispatch(
        role="planner", project="p", issue_number=1, ticket_id=1,
    )
    outcome = parse_outcome_from_stdout(stdout)
    assert outcome.kind == OutcomeKind.CLEAN
    assert outcome.summary == "stub ok"
