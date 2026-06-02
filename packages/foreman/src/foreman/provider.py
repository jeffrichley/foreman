"""Provider facade — single interface that all role modules dispatch through.

First (and currently only) concrete implementation is `AnthropicSDKProvider`.
The facade exists so future vendors (opencode, codex, cursor-cli) can plug
in via thin adapters without changing role-module code.

The `run_agent` contract is **Pydantic-first**: callers pass a Pydantic
``output_model`` class and receive a validated instance of that class. The
JSON-schema marshalling required by the SDK (which only accepts dicts) is
hoisted into the provider so role-runners (Planner / Reviewer / Worker /
Fixer) don't repeat ``model_json_schema()`` / ``model_validate()``
boilerplate.

This matches Anthropic's official claude-agent-sdk structured-output pattern
(see https://code.claude.com/docs/en/agent-sdk/structured-outputs).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class StructuredOutputRetryError(RuntimeError):
    """The SDK exhausted its retry budget trying to satisfy the schema.

    Surfaced when a ``ResultMessage`` arrives with subtype
    ``error_max_structured_output_retries``. The agent produced output the
    SDK could not coerce into the supplied JSON schema after its internal
    retry budget; raising this instead of the generic "missing output"
    error gives callers a specific failure mode to handle.
    """


class StructuredOutputMissingError(RuntimeError):
    """No ``ResultMessage`` carrying ``structured_output`` was ever produced.

    Indicates the agent loop terminated without a successful result — either
    the transport hung up, the agent exited early, or some path the SDK
    doesn't surface as a known error subtype.
    """


class ProviderFacade(ABC):
    """Abstract base for agent provider adapters."""

    @abstractmethod
    async def run_agent(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        allowed_tools: list[str],
        output_model: type[T],
        cwd: Path,
        max_turns: int = 1000,
        env: dict[str, str] | None = None,
    ) -> T:
        """Run an agent and return a validated instance of ``output_model``.

        Args:
            system_prompt: System-level instructions for the agent.
            user_prompt: The role-specific task prompt (issue body + context).
            allowed_tools: Tool names auto-approved (e.g., ["Read", "Edit", "Bash"]).
            output_model: Pydantic model class describing the agent's
                structured output. The provider generates the JSON schema
                from this class, passes it to the SDK, and validates the
                returned structured output back into an instance.
            cwd: Working directory for file ops (the per-ticket worktree).
            max_turns: Hard ceiling on agent loop iterations. Set absurdly
                high (1000) so the cap doesn't itself silently become the
                bug — see foreman#46 walk follow-up where the previous
                40-turn cap killed the Worker mid-spec with a vacuous
                ``did_check_pass=True``. Real budgets need real metrics
                first.
            env: Environment variables for the agent subprocess. When ``None``,
                the subprocess inherits the parent's full environment. When
                provided, callers are responsible for including parent vars
                they want preserved (e.g., ``{**os.environ, "GH_TOKEN": ...}``).
                Used by role dispatchers to inject per-role bot tokens so the
                agent's ``gh`` calls act as the bot identity, not the parent.

        Returns:
            An instance of ``output_model`` validated against the agent's
            structured output.

        Raises:
            StructuredOutputRetryError: The SDK exhausted retries trying to
                satisfy ``output_model``'s schema.
            StructuredOutputMissingError: The agent loop completed without
                ever producing a successful ``ResultMessage`` carrying
                ``structured_output``.
        """
