"""DaemonRunners — wraps the role functions for daemon-driven invocation.

The existing role functions (``foreman.roles.<role>.run_*``) take URLs and
return role-specific Pydantic models. The daemon, by contrast, deals in
``Ticket`` instances and expects a uniform ``RoleRunResult`` shape with
``new_labels`` + ``structured_output``.

This module bridges those two worlds:
- Build issue/PR URLs from tickets
- Call the role function
- Read back the post-run label state from the host (since roles continue
  to own label writes in v1)
- Return the unified ``RoleRunResult`` the daemon's worker expects

Also implements the daemon-internal merge actions (MergeSpecPR, MergeImplPR)
which don't exist as roles — they live here because they share the host
adapter.

Branch naming convention:
- spec PR head branch: ``foreman/issue-N``
- impl PR head branch: ``foreman/issue-N-impl``
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from foreman.config import Config, ProjectConfig
from foreman.dispatcher import Ticket
from foreman.provider import ProviderFacade
from foreman.providers.anthropic_sdk import AnthropicSDKProvider
from foreman.roles import fixer as _fixer_module
from foreman.roles import planner as _planner_module
from foreman.roles import reviewer as _reviewer_module
from foreman.roles import worker as _worker_module


@dataclass(frozen=True)
class RoleRunResult:
    """Uniform return type the daemon's worker expects."""

    new_labels: frozenset[str]
    structured_output: dict[str, Any] | None


class _HostLike(Protocol):
    def get_issue_labels(self, repo: str, issue_number: int) -> list[str]: ...
    def find_pr_for_branch(self, repo: str, branch: str) -> int | None: ...
    def merge_pull_request(self, repo: str, pr_number: int) -> None: ...
    def add_issue_label(self, repo: str, issue_number: int, label: str) -> None: ...
    def remove_issue_label(
        self, repo: str, issue_number: int, label: str
    ) -> None: ...
    def close_issue(self, repo: str, issue_number: int) -> None: ...


def _spec_branch(issue_number: int) -> str:
    return f"foreman/issue-{issue_number}"


def _impl_branch(issue_number: int) -> str:
    return f"foreman/issue-{issue_number}-impl"


def _issue_url(repo: str, issue_number: int) -> str:
    return f"https://github.com/{repo}/issues/{issue_number}"


def _pr_url(repo: str, pr_number: int) -> str:
    return f"https://github.com/{repo}/pull/{pr_number}"


class DaemonRunners:
    """Adapter exposing the runners protocol over the real role functions."""

    def __init__(
        self,
        *,
        host: _HostLike,
        worktrees_root: Path,
        provider: ProviderFacade | None = None,
        _planner: Any = None,
        _reviewer: Any = None,
        _fixer: Any = None,
        _worker: Any = None,
    ) -> None:
        """Construct the runner adapter.

        ``host`` is the GitHubDaemonHost used for post-run label reads,
        PR lookups, and merge actions.

        ``worktrees_root`` is the root under which per-ticket worktrees
        live (typically ``~/.foreman/worktrees``).

        ``provider`` is the LLM provider facade passed to each role
        function. Defaults to a fresh ``AnthropicSDKProvider`` when
        unset — matches the CLI's default wiring.

        The leading-underscore ``_planner``/``_reviewer``/``_fixer``/
        ``_worker`` kwargs are test seams — production code passes
        ``None`` and we resolve from the role modules at call time.
        """
        self._host = host
        self._worktrees_root = worktrees_root
        self._provider: ProviderFacade = provider or AnthropicSDKProvider()
        self._planner_fn = _planner or _planner_module.run_planner
        self._reviewer_fn = _reviewer or _reviewer_module.run_reviewer
        self._fixer_fn = _fixer or _fixer_module.run_fixer
        self._worker_fn = _worker or _worker_module.run_worker

    def _project(self, ticket: Ticket, config: Config) -> ProjectConfig:
        return config.projects[ticket.project_name]

    def _read_labels(self, ticket: Ticket, config: Config) -> frozenset[str]:
        project = self._project(ticket, config)
        return frozenset(self._host.get_issue_labels(project.repo, ticket.issue_number))

    async def run_planner(self, *, ticket: Ticket, config: Config) -> RoleRunResult:
        project = self._project(ticket, config)
        result = await self._planner_fn(
            issue_url=_issue_url(project.repo, ticket.issue_number),
            config=config,
            project_name=ticket.project_name,
            worktrees_root=self._worktrees_root,
            provider=self._provider,
        )
        return RoleRunResult(
            new_labels=self._read_labels(ticket, config),
            structured_output=_safe_dump(result),
        )

    async def run_reviewer(
        self, *, ticket: Ticket, config: Config, target: str
    ) -> RoleRunResult:
        project = self._project(ticket, config)
        branch = (
            _spec_branch(ticket.issue_number)
            if target == "spec_pr"
            else _impl_branch(ticket.issue_number)
        )
        pr_number = self._host.find_pr_for_branch(project.repo, branch)
        if pr_number is None:
            raise RuntimeError(
                f"No open PR found for branch {branch} on {project.repo}"
            )
        result = await self._reviewer_fn(
            pr_url=_pr_url(project.repo, pr_number),
            config=config,
            project_name=ticket.project_name,
            worktrees_root=self._worktrees_root,
            provider=self._provider,
        )
        return RoleRunResult(
            new_labels=self._read_labels(ticket, config),
            structured_output=_safe_dump(result),
        )

    async def run_fixer(
        self, *, ticket: Ticket, config: Config, target: str
    ) -> RoleRunResult:
        project = self._project(ticket, config)
        result = await self._fixer_fn(
            issue_url=_issue_url(project.repo, ticket.issue_number),
            config=config,
            project_name=ticket.project_name,
            worktrees_root=self._worktrees_root,
            provider=self._provider,
        )
        return RoleRunResult(
            new_labels=self._read_labels(ticket, config),
            structured_output=_safe_dump(result),
        )

    async def run_worker(self, *, ticket: Ticket, config: Config) -> RoleRunResult:
        project = self._project(ticket, config)
        result = await self._worker_fn(
            issue_url=_issue_url(project.repo, ticket.issue_number),
            config=config,
            project_name=ticket.project_name,
            worktrees_root=self._worktrees_root,
            provider=self._provider,
        )
        return RoleRunResult(
            new_labels=self._read_labels(ticket, config),
            structured_output=_safe_dump(result),
        )

    async def merge_spec_pr(self, *, ticket: Ticket, config: Config) -> RoleRunResult:
        project = self._project(ticket, config)
        branch = _spec_branch(ticket.issue_number)
        pr_number = self._host.find_pr_for_branch(project.repo, branch)
        if pr_number is None:
            raise RuntimeError(
                f"No open spec PR found for branch {branch} on {project.repo}"
            )
        self._host.merge_pull_request(project.repo, pr_number)
        # Advance label: remove spec-ready, add implementing-ready sentinel.
        self._host.remove_issue_label(
            project.repo, ticket.issue_number, "foreman:spec-ready"
        )
        self._host.add_issue_label(
            project.repo, ticket.issue_number, "foreman:implementing-ready"
        )
        return RoleRunResult(
            new_labels=self._read_labels(ticket, config),
            structured_output={"merged_spec_pr": pr_number},
        )

    async def merge_impl_pr(self, *, ticket: Ticket, config: Config) -> RoleRunResult:
        project = self._project(ticket, config)
        branch = _impl_branch(ticket.issue_number)
        pr_number = self._host.find_pr_for_branch(project.repo, branch)
        if pr_number is None:
            raise RuntimeError(
                f"No open impl PR found for branch {branch} on {project.repo}"
            )
        self._host.merge_pull_request(project.repo, pr_number)
        self._host.close_issue(project.repo, ticket.issue_number)
        return RoleRunResult(
            new_labels=self._read_labels(ticket, config),
            structured_output={"merged_impl_pr": pr_number},
        )


def _safe_dump(result: Any) -> dict[str, Any] | None:
    """Try to convert a role's return value to a dict for the SQLite audit row."""
    if result is None:
        return None
    if hasattr(result, "model_dump"):
        try:
            dumped = result.model_dump()
            return dumped if isinstance(dumped, dict) else None
        except Exception:
            return None
    if isinstance(result, dict):
        return result
    return None
