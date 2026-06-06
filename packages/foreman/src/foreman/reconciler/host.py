"""Protocol for the GitHub-side surface the action executor needs.

Real impl wraps PyGithub + the subprocess spawner. Tests use a recording
fake. Keeping the protocol thin keeps the action layer pure-data-shaped.
"""

from __future__ import annotations

from typing import Protocol

from foreman.config import MergeMechanism


class ReconcilerHost(Protocol):
    """The host side-effect surface for v3 actions."""

    def add_label(self, *, owner: str, repo: str, issue: int, label: str) -> None: ...
    def remove_label(self, *, owner: str, repo: str, issue: int, label: str) -> None: ...
    def post_comment(self, *, owner: str, repo: str, issue: int, body: str) -> None: ...
    def merge_pr(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        mechanism: MergeMechanism,
    ) -> None:
        """Merge a PR via the project's configured mechanism.

        ``mechanism`` is the EFFECTIVE per-project value resolved upstream
        in the executor (``ActionContext.merge_mechanism``). The host
        dispatches on it:

        - ``direct``: synchronous ``pr.merge()`` against the GH API.
          Today's behavior; fails with the strict-status-checks-policy
          error when main has moved since the PR opened.
        - ``queue``: defer to GitHub MergeQueue. Requires the base
          branch's branch-protection ruleset to have MergeQueue enabled
          AND the orchestrator-bot App to hold the ``Merge queues:
          write`` permission. See foreman#158.

        Wiring landed in foreman#161 (this PR); the queue-specific
        implementation lands in a follow-up.
        """
        ...
    def dispatch_role(
        self,
        *,
        role: str,
        target: str | None,
        owner: str,
        repo: str,
        issue: int,
        pr_number: int | None,
        start_log_id: int,
        project: str,
    ) -> int:
        """Spawn the role subprocess (planner|reviewer|fixer|worker). Returns the PID.

        ``target`` is the per-dispatch target for roles that act on more than
        one artifact shape — ``"spec_pr"`` or ``"impl_pr"`` for the Reviewer
        and Fixer. Planner and Worker pass ``None``. The host plumbs ``target``
        into the subprocess argv via ``--target`` so the role's CLI dispatches
        the right prompt + entry-label check (foreman v3 rescue Stage 2).

        ``start_log_id`` is the id of the 'running' row the executor wrote
        immediately before calling this method. The host owns writing the
        termination row when the subprocess exits — see
        ``V3GitHubHost._track_subprocess_completion``.

        ``project`` is the project name the dispatched action belongs to —
        callers MUST pass the project of the snapshot that fired the rule,
        not a host-wide default. One host instance serves all registered
        projects; baking a single project_name into the host produces
        cross-project mis-dispatch (foreman v3 dogfood bug, 2026-06-04).
        Plumbed into the subprocess via ``--project``.
        """
        ...
