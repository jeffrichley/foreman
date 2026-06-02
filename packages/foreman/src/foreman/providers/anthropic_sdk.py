"""Anthropic Agent SDK adapter for the provider facade.

Uses ``claude_agent_sdk.query()`` to run an agent with the supplied prompt,
tools, working directory, and Pydantic ``output_model``. The schema is
generated from the model on the way in; the returned ``structured_output``
dict is validated back into an instance of the model on the way out.

This adapter also inspects ``ResultMessage.subtype`` so callers see specific
exceptions for SDK-recognised failure modes (retry exhaustion) rather than a
generic "no structured output" error.

System prompts are routed through ``--system-prompt-file`` (a path) instead
of ``--system-prompt`` (the prompt text) — the SDK's subprocess transport
passes the latter as a command-line argument, and Windows' ``CreateProcess``
caps the command line at 8191 chars. The Worker role's composed prompt
(adapter preamble + four vendored superpowers + role contract) exceeds
that. See foreman#50 + claude-agent-sdk #501.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
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

# Foreman-owned cache of role system prompts. Lives next to the other
# foreman runtime dirs (``worktrees/``, ``keys/``, ``stats/``) so a
# leaked file (daemon crash mid-query) is discoverable in a predictable
# place. Per-call files are deleted in a ``try/finally``; the dir is
# created lazily.
_PROMPTS_DIR = Path.home() / ".foreman" / "prompts"


@contextmanager
def _system_prompt_file(prompt: str, *, hint: str) -> Iterator[Path]:
    """Write ``prompt`` to a uniquely-named file under
    ``~/.foreman/prompts`` and yield its path. The file is deleted when
    the block exits, even on exception.

    ``hint`` becomes part of the filename so an operator inspecting
    ``~/.foreman/prompts/`` after a crash can tell who owned each file.
    The cwd's basename (e.g. ``impl-46`` or ``46``) is a natural hint;
    any short slug works.
    """
    _PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _PROMPTS_DIR / f"{hint}-{uuid.uuid4().hex[:8]}.md"
    path.write_text(prompt, encoding="utf-8")
    try:
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


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
        hint = cwd.name or "role"
        with _system_prompt_file(system_prompt, hint=hint) as sp_path:
            options_kwargs: dict[str, Any] = dict(
                system_prompt={"type": "file", "path": str(sp_path)},
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
