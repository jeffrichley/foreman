"""Real-role dispatcher — routes Action.kind to the corresponding role runner.

Sits between the worker's abstract ``RoleDispatcher`` protocol and the
concrete ``run_planner`` / ``run_reviewer`` / ``run_fixer`` / ``run_worker``
implementations in ``foreman.roles``. Also handles ``MERGE_SPEC_PR`` and
``MERGE_IMPL_PR`` which are daemon-internal (not role-based).

The ``runners`` parameter is an object exposing async methods
``run_planner``, ``run_reviewer``, ``run_fixer``, ``run_worker``,
``merge_spec_pr``, ``merge_impl_pr``. Production wiring binds it to the
real role modules; tests inject a mock.
"""

from __future__ import annotations

from typing import Protocol

from foreman.config import Config
from foreman.dispatcher import Action, ActionKind, Ticket
from foreman.worker import RoleResult


class _RoleRun(Protocol):
    new_labels: list[str] | frozenset[str]
    structured_output: dict | None


class _RunnersProtocol(Protocol):
    async def run_planner(self, *, ticket: Ticket, config: Config) -> _RoleRun: ...
    async def run_reviewer(
        self, *, ticket: Ticket, config: Config, target: str
    ) -> _RoleRun: ...
    async def run_fixer(
        self, *, ticket: Ticket, config: Config, target: str
    ) -> _RoleRun: ...
    async def run_worker(self, *, ticket: Ticket, config: Config) -> _RoleRun: ...
    async def merge_spec_pr(self, *, ticket: Ticket, config: Config) -> _RoleRun: ...
    async def merge_impl_pr(self, *, ticket: Ticket, config: Config) -> _RoleRun: ...


class RealRoleDispatcher:
    """Routes Actions to concrete role runners."""

    def __init__(self, *, config: Config, runners: _RunnersProtocol) -> None:
        self.config = config
        self.runners = runners

    async def dispatch(self, *, ticket: Ticket, action: Action) -> RoleResult:
        run_result = await self._invoke(ticket=ticket, action=action)
        return RoleResult(
            new_labels=frozenset(run_result.new_labels),
            structured_output=run_result.structured_output,
            outcome="success",
        )

    async def _invoke(self, *, ticket: Ticket, action: Action) -> _RoleRun:
        if action.kind == ActionKind.RUN_PLANNER:
            return await self.runners.run_planner(ticket=ticket, config=self.config)
        if action.kind == ActionKind.RUN_REVIEWER_SPEC:
            return await self.runners.run_reviewer(
                ticket=ticket, config=self.config, target="spec_pr"
            )
        if action.kind == ActionKind.RUN_REVIEWER_IMPL:
            return await self.runners.run_reviewer(
                ticket=ticket, config=self.config, target="impl_pr"
            )
        if action.kind == ActionKind.RUN_FIXER_SPEC:
            return await self.runners.run_fixer(
                ticket=ticket, config=self.config, target="spec_pr"
            )
        if action.kind == ActionKind.RUN_FIXER_IMPL:
            return await self.runners.run_fixer(
                ticket=ticket, config=self.config, target="impl_pr"
            )
        if action.kind == ActionKind.RUN_WORKER:
            return await self.runners.run_worker(ticket=ticket, config=self.config)
        if action.kind == ActionKind.MERGE_SPEC_PR:
            return await self.runners.merge_spec_pr(ticket=ticket, config=self.config)
        if action.kind == ActionKind.MERGE_IMPL_PR:
            return await self.runners.merge_impl_pr(ticket=ticket, config=self.config)
        raise ValueError(f"Unknown action kind: {action.kind}")
