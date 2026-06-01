"""Anthropic Agent SDK adapter for the provider facade.

Uses ``claude_agent_sdk.query()`` to run an agent with the supplied prompt,
tools, working directory, and Pydantic ``output_model``. The schema is
generated from the model on the way in; the returned ``structured_output``
dict is validated back into an instance of the model on the way out.

This adapter also inspects ``ResultMessage.subtype`` so callers see specific
exceptions for SDK-recognised failure modes (retry exhaustion) rather than a
generic "no structured output" error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from pydantic import BaseModel

from foreman.provider import (
    ProviderFacade,
    StructuredOutputMissingError,
    StructuredOutputRetryError,
)

T = TypeVar("T", bound=BaseModel)


class AnthropicSDKProvider(ProviderFacade):
    """ProviderFacade implementation backed by the Anthropic Agent SDK."""

    async def run_agent(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        allowed_tools: list[str],
        output_model: type[T],
        cwd: Path,
        max_turns: int = 40,
        env: dict[str, str] | None = None,
    ) -> T:
        schema = output_model.model_json_schema()
        options_kwargs: dict[str, Any] = dict(
            system_prompt=system_prompt,
            cwd=str(cwd),
            allowed_tools=allowed_tools,
            permission_mode="acceptEdits",
            max_turns=max_turns,
            output_format={"type": "json_schema", "schema": schema},
        )
        if env is not None:
            options_kwargs["env"] = env
        options = ClaudeAgentOptions(**options_kwargs)

        async for message in query(prompt=user_prompt, options=options):
            if not isinstance(message, ResultMessage):
                continue
            # SDK subtype taxonomy (verified against
            # claude_agent_sdk types.ResultMessage — `subtype: str`):
            #   "success" + structured_output → validated instance
            #   "error_max_structured_output_retries" → SDK gave up
            #   anything else → keep looping; the SDK may emit more results
            if message.subtype == "success" and message.structured_output is not None:
                return output_model.model_validate(message.structured_output)
            if message.subtype == "error_max_structured_output_retries":
                raise StructuredOutputRetryError(
                    "Anthropic Agent SDK exhausted its retry budget trying to "
                    f"satisfy the schema for {output_model.__name__}. "
                    f"Errors reported: {message.errors!r}"
                )

        raise StructuredOutputMissingError(
            "Anthropic Agent SDK did not return a successful ResultMessage "
            f"carrying structured_output for {output_model.__name__}"
        )
