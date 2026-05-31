"""Per-role identity registry — builds PyGithub clients per bot identity.

Each role gets its own Github() instance authenticated as the role's bot.
For walking skeleton: only the planner role is wired. Reviewer/fixer/worker
will be added during thickening.
"""

from __future__ import annotations

from github import Auth, Github

from foreman.config import ProjectConfig


class IdentityRegistry:
    """Holds per-role PyGithub clients for one project."""

    def __init__(self, project: ProjectConfig) -> None:
        self._project = project
        self._clients: dict[str, Github] = {}

    def get_client(self, role: str) -> Github:
        if role in self._clients:
            return self._clients[role]
        token = self._resolve_token(role)
        client = Github(auth=Auth.Token(token))
        self._clients[role] = client
        return client

    def _resolve_token(self, role: str) -> str:
        if role == "planner":
            return self._project.bots.resolve_planner_token()
        raise ValueError(
            f"Unknown role: {role!r}. Walking skeleton only supports 'planner'."
        )
