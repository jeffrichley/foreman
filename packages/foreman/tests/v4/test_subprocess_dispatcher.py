"""SubprocessRoleDispatcher — shells out to foreman <role>-v4 for v4 dispatch."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from foreman.v4.subprocess_dispatcher import (
    RoleSubprocessError,
    SubprocessRoleDispatcher,
)


def _stub_identity():
    """Builds a fake identity module exposing get_role_token."""
    mod = MagicMock()
    mod.get_role_token.return_value = "ghp_TESTTOKEN"
    return mod


def test_planner_dispatch_invokes_foreman_plan_v4():
    completed = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=(
            'log lines\n'
            'FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"ok"}\n'
        ),
        stderr="",
    )
    with patch("subprocess.run", return_value=completed) as run:
        dispatcher = SubprocessRoleDispatcher(
            foreman_cli=["foreman"], identity=_stub_identity(),
        )
        stdout = dispatcher.dispatch(
            role="planner", project="p", issue_number=1, ticket_id=1,
        )
    assert "FOREMAN_OUTCOME:" in stdout
    args = run.call_args
    cmd = args[0][0] if args[0] else args[1].get("args")
    assert "plan-v4" in cmd
    assert "--project" in cmd
    assert "1" in cmd
    # GH_TOKEN injected via env, not arg
    env = args[1].get("env") or {}
    assert env.get("GH_TOKEN") == "ghp_TESTTOKEN"


@pytest.mark.parametrize(
    "role,subcmd,target",
    [
        ("planner", "plan-v4", None),
        ("reviewer-spec", "review-v4", "spec"),
        ("reviewer-impl", "review-v4", "impl"),
        ("fixer-spec", "fix-v4", "spec"),
        ("fixer-impl", "fix-v4", "impl"),
        ("worker", "implement-v4", None),
    ],
)
def test_role_to_subcommand_mapping(role, subcmd, target):
    completed = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout='FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"x"}\n',
        stderr="",
    )
    with patch("subprocess.run", return_value=completed) as run:
        SubprocessRoleDispatcher(
            foreman_cli=["foreman"], identity=_stub_identity(),
        ).dispatch(role=role, project="p", issue_number=1, ticket_id=1)
    cmd = run.call_args[0][0]
    assert subcmd in cmd
    if target is not None:
        assert "--target" in cmd
        assert target in cmd


def test_subprocess_nonzero_with_error_outcome_returns_stdout():
    """Non-zero exit + ERROR outcome → dispatcher returns the stdout; the
    state machine's verify hook decides what ERROR means. No exception
    raised at the dispatcher layer for cases that emitted an outcome."""
    completed = subprocess.CompletedProcess(
        args=[], returncode=1,
        stdout='FOREMAN_OUTCOME:{"kind":"error","confidence":"high","summary":"boom"}\n',
        stderr="something went sideways",
    )
    with patch("subprocess.run", return_value=completed):
        dispatcher = SubprocessRoleDispatcher(
            foreman_cli=["foreman"], identity=_stub_identity(),
        )
        stdout = dispatcher.dispatch(
            role="planner", project="p", issue_number=1, ticket_id=1,
        )
        assert '"kind":"error"' in stdout


def test_subprocess_nonzero_without_outcome_raises():
    """If the subprocess died without writing a marker, that's a hard error."""
    completed = subprocess.CompletedProcess(
        args=[], returncode=137, stdout="killed\n", stderr="OOM",
    )
    with patch("subprocess.run", return_value=completed):
        dispatcher = SubprocessRoleDispatcher(
            foreman_cli=["foreman"], identity=_stub_identity(),
        )
        with pytest.raises(RoleSubprocessError) as exc:
            dispatcher.dispatch(
                role="planner", project="p", issue_number=1, ticket_id=1,
            )
        assert "137" in str(exc.value)


def test_unknown_role_raises_value_error():
    with patch("subprocess.run"):
        dispatcher = SubprocessRoleDispatcher(
            foreman_cli=["foreman"], identity=_stub_identity(),
        )
        with pytest.raises(ValueError, match="unknown role"):
            dispatcher.dispatch(
                role="not-a-role", project="p", issue_number=1, ticket_id=1,
            )


def test_identity_token_injected_per_role():
    """get_role_token is called with the role name; result lands in GH_TOKEN."""
    identity = MagicMock()
    identity.get_role_token.side_effect = lambda r: f"token-for-{r}"
    completed = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout='FOREMAN_OUTCOME:{"kind":"clean","confidence":"high","summary":"x"}\n',
        stderr="",
    )
    with patch("subprocess.run", return_value=completed) as run:
        SubprocessRoleDispatcher(
            foreman_cli=["foreman"], identity=identity,
        ).dispatch(role="reviewer-spec", project="p", issue_number=1, ticket_id=1)
    identity.get_role_token.assert_called_once_with("reviewer-spec")
    env = run.call_args[1].get("env") or {}
    assert env["GH_TOKEN"] == "token-for-reviewer-spec"
