"""Provider facade — single interface that all role modules dispatch through.

First (and currently only) concrete implementation is `AnthropicSDKProvider`.
The facade exists so future vendors (opencode, codex, cursor-cli) can plug
in via thin adapters without changing role-module code.

The `run_agent` contract returns a parsed dict matching the supplied
JSON schema. Role modules pass their Pydantic model's `model_json_schema()`
output as the schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class ProviderFacade(ABC):
    """Abstract base for agent provider adapters."""

    @abstractmethod
    async def run_agent(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        allowed_tools: list[str],
        output_schema: dict[str, Any],
        cwd: Path,
        max_turns: int = 40,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run an agent and return its structured output as a dict.

        Args:
            system_prompt: System-level instructions for the agent.
            user_prompt: The role-specific task prompt (issue body + context).
            allowed_tools: Tool names auto-approved (e.g., ["Read", "Edit", "Bash"]).
            output_schema: JSON schema the agent's output must match.
            cwd: Working directory for file ops (the per-ticket worktree).
            max_turns: Safety cap on agent loop iterations.
            env: Environment variables for the agent subprocess. When ``None``,
                the subprocess inherits the parent's full environment. When
                provided, callers are responsible for including parent vars
                they want preserved (e.g., ``{**os.environ, "GH_TOKEN": ...}``).
                Used by role dispatchers to inject per-role bot tokens so the
                agent's ``gh`` calls act as the bot identity, not the parent.

        Returns:
            Dict matching `output_schema`.

        Raises:
            RuntimeError: If the agent fails to produce schema-valid output.
        """
