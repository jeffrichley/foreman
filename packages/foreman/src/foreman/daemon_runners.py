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
- impl PR head branch: ``foreman/impl-N``
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from foreman.branches import impl_branch, spec_branch
from foreman.config import Config, ProjectConfig
from foreman.dispatcher import Ticket
from foreman.provider import ProviderFacade
from foreman.providers import make_provider
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
    def remove_issue_label(self, repo: str, issue_number: int, label: str) -> None: ...
    def close_issue(self, repo: str, issue_number: int) -> None: ...
    def get_pr_base_ref(self, repo: str, pr_number: int) -> str: ...
    def is_pr_merged_for_branch(self, repo: str, branch: str) -> bool: ...
    def retarget_pr_base(self, repo: str, pr_number: int, new_base: str) -> None: ...
    def get_default_branch(self, repo: str) -> str: ...


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
        function. Defaults to ``make_provider()`` when unset — the
        production factory that wires the deployed recovery chain
        (foreman#266) — matches the CLI's default wiring.

        The leading-underscore ``_planner``/``_reviewer``/``_fixer``/
        ``_worker`` kwargs are test seams — production code passes
        ``None`` and we resolve from the role modules at call time.
        """
        self._host = host
        self._worktrees_root = worktrees_root
        self._provider: ProviderFacade = provider or make_provider()
        self._planner_fn = _planner or _planner_module.run_planner
        self._reviewer_fn = _reviewer or _reviewer_module.run_reviewer
        self._fixer_fn = _fixer or _fixer_module.run_fixer
        self._worker_fn = _worker or _worker_module.run_worker

    def _project(self, ticket: Ticket, config: Config) -> ProjectConfig:
        return config.projects[ticket.project_name]

    async def run_planner(self, *, ticket: Ticket, config: Config) -> RoleRunResult:
        project = self._project(ticket, config)
        result = await self._planner_fn(
            issue_url=_issue_url(project.repo, ticket.issue_number),
            config=config,
            project_name=ticket.project_name,
            worktrees_root=self._worktrees_root,
            provider=self._provider,
        )
        # foreman#91: the role-returned ``final_labels`` is the
        # authoritative post-run label set, computed in-process by
        # the role from its known mutations. We trust that signal
        # over a fresh ``host.get_issue_labels`` GET which would
        # race GitHub's eventual-consistency window and could
        # return the OLD labels even though the role's writes
        # succeeded — producing stale-snapshot re-dispatches at the
        # next worker iteration.
        return RoleRunResult(
            new_labels=frozenset(result.final_labels),
            structured_output=_safe_dump(result.llm_output),
        )

    async def run_reviewer(self, *, ticket: Ticket, config: Config, target: str) -> RoleRunResult:
        project = self._project(ticket, config)
        branch = (
            spec_branch(ticket.issue_number)
            if target == "spec_pr"
            else impl_branch(ticket.issue_number)
        )
        pr_number = self._host.find_pr_for_branch(project.repo, branch)
        if pr_number is None:
            raise RuntimeError(f"No open PR found for branch {branch} on {project.repo}")
        result = await self._reviewer_fn(
            pr_url=_pr_url(project.repo, pr_number),
            config=config,
            project_name=ticket.project_name,
            worktrees_root=self._worktrees_root,
            provider=self._provider,
        )
        return RoleRunResult(
            new_labels=frozenset(result.final_labels),
            structured_output=_safe_dump(result.llm_output),
        )

    async def run_fixer(self, *, ticket: Ticket, config: Config, target: str) -> RoleRunResult:
        project = self._project(ticket, config)
        result = await self._fixer_fn(
            issue_url=_issue_url(project.repo, ticket.issue_number),
            config=config,
            project_name=ticket.project_name,
            worktrees_root=self._worktrees_root,
            provider=self._provider,
            target=target,
        )
        return RoleRunResult(
            new_labels=frozenset(result.final_labels),
            structured_output=_safe_dump(result.llm_output),
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
            new_labels=frozenset(result.final_labels),
            structured_output=_safe_dump(result.llm_output),
        )

    async def merge_spec_pr(self, *, ticket: Ticket, config: Config) -> RoleRunResult:
        project = self._project(ticket, config)
        branch = spec_branch(ticket.issue_number)
        pr_number = self._host.find_pr_for_branch(project.repo, branch)
        if pr_number is None:
            raise RuntimeError(f"No open spec PR found for branch {branch} on {project.repo}")
        self._host.merge_pull_request(project.repo, pr_number)
        # Advance label: remove spec-ready, add implementing-ready sentinel.
        self._host.remove_issue_label(project.repo, ticket.issue_number, "foreman:spec-ready")
        self._host.add_issue_label(project.repo, ticket.issue_number, "foreman:implementing-ready")
        # foreman#91: compute the post-merge label set deterministically
        # from the pre-merge snapshot in ``ticket.labels`` + the merge
        # transitions above. Avoids the eventual-consistency hazard of a
        # remote GET right after the writes (different client identity
        # for the merge writes vs. the read in v1 — see
        # ``DaemonRunners._read_labels``'s now-removed implementation).
        final_labels = frozenset(
            (set(ticket.labels) - {"foreman:spec-ready"}) | {"foreman:implementing-ready"}
        )
        return RoleRunResult(
            new_labels=final_labels,
            structured_output={"merged_spec_pr": pr_number},
        )

    async def merge_impl_pr(self, *, ticket: Ticket, config: Config) -> RoleRunResult:
        """Merge the impl PR, retargeting its base to the default branch first
        if it still points at the spec branch and the spec PR has merged.

        Why the retarget step (issue #62): the Worker opens impl PRs with
        ``base=foreman/issue-N`` per the stacked-PR pattern. Once the
        spec PR merges, calling ``pr.merge() + delete_branch=True``
        without retargeting produces a squash commit on the
        about-to-be-deleted spec branch — an orphan commit unreachable
        from main. The PR is reported MERGED but the work is lost
        (foreman#49 → recovery PR #61, caught 2026-06-02). Retargeting
        the impl PR's base to main before merge ensures the squash
        commit lands on a reachable ref.

        Conditional on two checks: (1) impl PR's current base IS the
        spec branch — skip if already retargeted, for idempotency under
        crash re-enqueue; (2) the spec PR has merged — skip if the spec
        is still pending, since retargeting to main and merging would
        land impl changes that depend on un-landed spec changes.
        """
        project = self._project(ticket, config)
        branch = impl_branch(ticket.issue_number)
        pr_number = self._host.find_pr_for_branch(project.repo, branch)
        if pr_number is None:
            raise RuntimeError(f"No open impl PR found for branch {branch} on {project.repo}")
        # Retarget impl PR to default branch before merge if it still
        # points at the spec branch AND the spec PR has merged. See
        # issue #62 for the ghost-merge failure mode this guards against.
        spec_branch_name = spec_branch(ticket.issue_number)
        current_base = self._host.get_pr_base_ref(project.repo, pr_number)
        if current_base == spec_branch_name and self._host.is_pr_merged_for_branch(
            project.repo, spec_branch_name
        ):
            default_branch = self._host.get_default_branch(project.repo)
            self._host.retarget_pr_base(project.repo, pr_number, default_branch)
        self._host.merge_pull_request(project.repo, pr_number)
        self._host.close_issue(project.repo, ticket.issue_number)
        # foreman#91: compute the post-merge label set deterministically.
        # The issue is closed; the only label transition relative to the
        # queue's view is dropping ``foreman:ready-for-merge``. With no
        # actionable label remaining, ``next_action`` returns ``None`` and
        # the queue parks the ticket — correct terminal state.
        final_labels = frozenset(set(ticket.labels) - {"foreman:ready-for-merge"})
        return RoleRunResult(
            new_labels=final_labels,
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
