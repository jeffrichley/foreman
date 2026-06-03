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
        owner: str,
        repo: str,
        issue: int,
        pr_number: int | None,
    ) -> int:
        """Spawn the role subprocess (planner|reviewer|fixer|worker). Returns the PID."""
        ...
