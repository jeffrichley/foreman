"""Protocol for the GitHub-side surface the action executor needs.

Real impl wraps PyGithub + the subprocess spawner. Tests use a recording
fake. Keeping the protocol thin keeps the action layer pure-data-shaped.
"""

from __future__ import annotations

from typing import Protocol


class ReconcilerHost(Protocol):
    """The host side-effect surface for v3 actions."""

    def add_label(self, *, owner: str, repo: str, issue: int, label: str) -> None: ...
    def remove_label(self, *, owner: str, repo: str, issue: int, label: str) -> None: ...
    def post_comment(self, *, owner: str, repo: str, issue: int, body: str) -> None: ...
    def merge_pr(self, *, owner: str, repo: str, pr_number: int) -> None: ...
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
        """
        ...
