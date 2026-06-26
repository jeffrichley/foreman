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
        state_instance_id: int | None = None,
        session_id: str | None = None,
        resume: bool = False,
    ) -> str:
        """Return the role subprocess's stdout. Must contain FOREMAN_OUTCOME:.

        ``state_instance_id`` (foreman#367) is the current state-instance
        row id; the dispatcher exports it as ``FOREMAN_STATE_INSTANCE_ID``
        in the subprocess env so the role-core dedup-key construction is
        stable across retries on the same state instance. Default
        ``None`` for direct-CLI invocation outside the v4 dispatcher.

        ``session_id`` + ``resume`` (crash-recovery resume arm) are the
        inert plumbing for continuing an interrupted role's Claude
        session: when ``session_id`` is not None the dispatcher exports
        ``FOREMAN_SESSION_ID``; when ``resume`` is True it also exports
        ``FOREMAN_RESUME_SESSION_ID``. Both default off — nothing in this
        layer decides when to resume.
        """


class FakeRoleDispatcher:
    """In-memory dispatcher: maps (role, project, issue_number) -> canned stdout."""

    def __init__(self, *, responses: dict[tuple[str, str, int], str]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, int, int]] = []
        # Crash-recovery resume arm: per-call record of the resume
        # plumbing so tests can assert what the state machine threaded
        # down. Kept separate from ``calls`` (whose 4-tuple shape existing
        # equality assertions depend on).
        self.resume_calls: list[tuple[str | None, bool]] = []

    def dispatch(
        self,
        *,
        role: str,
        project: str,
        issue_number: int,
        ticket_id: int,
        state_instance_id: int | None = None,
        session_id: str | None = None,
        resume: bool = False,
    ) -> str:
        del state_instance_id  # not relevant to the in-memory fake
        self.calls.append((role, project, issue_number, ticket_id))
        self.resume_calls.append((session_id, resume))
        key = (role, project, issue_number)
        try:
            return self._responses[key]
        except KeyError as exc:
            raise RoleNotConfiguredError(f"no canned response for {key}") from exc
