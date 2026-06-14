"""SubprocessRoleDispatcher — production RoleDispatcher impl.

Shells out to ``foreman <subcmd>-v4 ...`` with the role's identity token
injected as GH_TOKEN. Returns the subprocess's stdout for the state
machine's verify hook to parse.

The mapping from v4 role names to CLI subcommands lives in
``_ROLE_TO_INVOCATION``. Adding a new role = one entry there.

Phase 8 strips the ``-v4`` suffix once the legacy CLI commands are
deleted; that's the only change required here at cutover.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Protocol

from foreman.v4.outcome import OUTCOME_MARKER


class IdentityProvider(Protocol):
    def get_role_token(self, role: str) -> str: ...


class RoleSubprocessError(RuntimeError):
    """Subprocess exited non-zero AND did not emit a FOREMAN_OUTCOME: line."""


@dataclass(frozen=True)
class _Invocation:
    subcommand: str
    target: str | None


_ROLE_TO_INVOCATION: dict[str, _Invocation] = {
    "planner":       _Invocation(subcommand="plan",      target=None),
    "reviewer-spec": _Invocation(subcommand="review",    target="spec"),
    "reviewer-impl": _Invocation(subcommand="review",    target="impl"),
    "fixer-spec":    _Invocation(subcommand="fix",       target="spec"),
    "fixer-impl":    _Invocation(subcommand="fix",       target="impl"),
    "worker":        _Invocation(subcommand="implement", target=None),
}


class SubprocessRoleDispatcher:
    def __init__(
        self,
        *,
        foreman_cli: list[str],
        identity: IdentityProvider,
        timeout_seconds: int = 600,
    ) -> None:
        self._foreman_cli = foreman_cli
        self._identity = identity
        self._timeout = timeout_seconds

    def dispatch(
        self, *, role: str, project: str, issue_number: int, ticket_id: int,
    ) -> str:
        try:
            inv = _ROLE_TO_INVOCATION[role]
        except KeyError as exc:
            raise ValueError(f"unknown role: {role}") from exc

        cmd = [
            *self._foreman_cli, inv.subcommand,
            "--project", project,
            "--issue-number", str(issue_number),
        ]
        if inv.target is not None:
            cmd += ["--target", inv.target]

        env = dict(os.environ)
        env["GH_TOKEN"] = self._identity.get_role_token(role)

        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env,
            timeout=self._timeout,
        )
        if result.returncode != 0 and OUTCOME_MARKER not in result.stdout:
            raise RoleSubprocessError(
                f"role={role} exited {result.returncode} without "
                f"emitting an outcome; stderr={result.stderr[:500]!r}"
            )
        return result.stdout
