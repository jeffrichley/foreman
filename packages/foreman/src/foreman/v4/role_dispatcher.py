"""RoleDispatcher — the seam between v4 state machine and role subprocesses.

Concrete states do not import PyGithub or invoke subprocess directly. They
call dispatcher.dispatch(role=..., project=..., issue_number=...) and the
real implementation (Phase 4) shells out to ``foreman <role> ...`` with the
appropriate per-role identity.
"""

from __future__ import annotations

from typing import Protocol


class RoleNotConfiguredError(LookupError):
    """The fake had no canned response for this (role, project, issue_number)."""


class RoleDispatcher(Protocol):
    def dispatch(
        self,
        *,
        role: str,
        project: str,
        issue_number: int,
        ticket_id: int,
    ) -> str:
        """Return the role subprocess's stdout. Must contain FOREMAN_OUTCOME:."""


class FakeRoleDispatcher:
    """In-memory dispatcher: maps (role, project, issue_number) -> canned stdout."""

    def __init__(self, *, responses: dict[tuple[str, str, int], str]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, int, int]] = []

    def dispatch(
        self,
        *,
        role: str,
        project: str,
        issue_number: int,
        ticket_id: int,
    ) -> str:
        self.calls.append((role, project, issue_number, ticket_id))
        key = (role, project, issue_number)
        try:
            return self._responses[key]
        except KeyError as exc:
            raise RoleNotConfiguredError(f"no canned response for {key}") from exc
