"""Anthropic Agent SDK adapter for the provider facade.

Uses `claude_agent_sdk.query()` to run an agent with the supplied prompt,
tools, working directory, and structured-output schema. Returns the parsed
structured output as a dict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from foreman.provider import ProviderFacade


class AnthropicSDKProvider(ProviderFacade):
    """ProviderFacade implementation backed by the Anthropic Agent SDK."""

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
        options_kwargs: dict[str, Any] = dict(
            system_prompt=system_prompt,
            cwd=str(cwd),
            allowed_tools=allowed_tools,
            permission_mode="acceptEdits",
            max_turns=max_turns,
            output_format={"type": "json_schema", "schema": output_schema},
        )
        if env is not None:
            options_kwargs["env"] = env
        options = ClaudeAgentOptions(**options_kwargs)
        structured: dict[str, Any] | None = None
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, ResultMessage) and message.structured_output:
                structured = message.structured_output
        if structured is None:
            raise RuntimeError(
                "Anthropic Agent SDK did not return structured_output matching schema"
            )
        return structured
