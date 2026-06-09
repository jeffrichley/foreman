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

import logging
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from pydantic import BaseModel

from foreman.provider import (
    ProviderAuthError,
    ProviderFacade,
    StructuredOutputMissingError,
    StructuredOutputRetryError,
    UsageInfo,
)

T = TypeVar("T", bound=BaseModel)

log = logging.getLogger(__name__)

# foreman#227 (2026-06-08): when the Claude Code subprocess fails to
# authenticate (typically because the container's OAuth token expired
# while the host's was rotated), the SDK surfaces the failure as a
# generic ``Exception`` whose message starts with
# ``"Claude Code returned an error result: ..."``. The underlying API
# error is a 401, but the SDK's error-text fallback substitutes the
# protocol ``subtype`` (which says "success") for the missing
# ``errors`` field, producing the absurd
# ``"Claude Code returned an error result: success"``. We pattern-match
# the prefix so we don't depend on the (confusing) trailing word.
_SDK_AUTH_ERROR_PREFIX = "Claude Code returned an error result"

# Container-internal paths where the daemon's Compose secrets land. In
# the dev/test environment these paths don't exist and the refresh
# helper is a no-op.
_CLAUDE_CREDS_LIVE_SRC = Path("/run/secrets/claude_credentials")
_CLAUDE_CREDS_LOCAL_DST = Path("/root/.claude/.credentials.json")


def _maybe_refresh_container_creds() -> bool:
    """Re-copy the container's local Claude credentials from the live
    Compose-secret bind mount.

    Returns True if a fresh copy was installed; False otherwise (paths
    don't exist outside the container, or the local copy is already at
    least as new as the source). This pairs with the periodic refresh
    loop in ``docker/entrypoint.sh`` — entrypoint loop catches expiry
    proactively, this catches the rare in-flight expiry where a role
    runner started a query just before the loop's next tick.
    """
    if not (_CLAUDE_CREDS_LIVE_SRC.exists() and _CLAUDE_CREDS_LOCAL_DST.exists()):
        return False
    src_mtime = _CLAUDE_CREDS_LIVE_SRC.stat().st_mtime
    dst_mtime = _CLAUDE_CREDS_LOCAL_DST.stat().st_mtime
    if src_mtime <= dst_mtime:
        return False
    shutil.copyfile(_CLAUDE_CREDS_LIVE_SRC, _CLAUDE_CREDS_LOCAL_DST)
    _CLAUDE_CREDS_LOCAL_DST.chmod(0o600)
    return True

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
        max_turns: int = 1000,
        env: dict[str, str] | None = None,
    ) -> tuple[T, UsageInfo]:
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

            # foreman#227 (2026-06-08): wrap the SDK iteration in a
            # one-shot retry guarded on auth failure. When the
            # container's local credentials file goes stale (the
            # entrypoint's periodic loop hasn't caught it yet), the
            # SDK raises a generic Exception whose message starts with
            # ``"Claude Code returned an error result"`` (the underlying
            # cause is a 401 auth failure). We attempt one refresh from
            # the live Compose secret + one retry before surfacing as
            # ``ProviderAuthError``. Non-auth exceptions propagate
            # unchanged.
            try:
                return await self._iterate_query(
                    user_prompt=user_prompt, options=options, output_model=output_model
                )
            except Exception as e:
                if not str(e).startswith(_SDK_AUTH_ERROR_PREFIX):
                    raise
                refreshed = _maybe_refresh_container_creds()
                log.warning(
                    "Caught SDK auth-error pattern (%r); refreshed=%s; retrying once",
                    str(e),
                    refreshed,
                )
                try:
                    return await self._iterate_query(
                        user_prompt=user_prompt,
                        options=options,
                        output_model=output_model,
                    )
                except Exception as retry_exc:
                    if str(retry_exc).startswith(_SDK_AUTH_ERROR_PREFIX):
                        raise ProviderAuthError(
                            "Anthropic Agent SDK failed authentication twice "
                            "in a row, including after a credentials refresh. "
                            "The container's Compose-mounted secret is likely "
                            "also stale on the host side. Original error: "
                            f"{retry_exc!r}"
                        ) from retry_exc
                    raise

    async def _iterate_query(
        self,
        *,
        user_prompt: str,
        options: ClaudeAgentOptions,
        output_model: type[T],
    ) -> tuple[T, UsageInfo]:
        """Iterate the SDK query stream and validate the structured output.

        Split out from ``run_agent`` so the auth-retry wrapper can call it
        twice. Caller is responsible for the ``ClaudeAgentOptions``
        construction + the system-prompt-file lifecycle.

        Returns ``(validated_output, usage_info)`` where ``usage_info``
        is reconstructed from the same ``ResultMessage`` that carried
        the structured output (foreman#227).
        """
        async for message in query(prompt=user_prompt, options=options):
            if not isinstance(message, ResultMessage):
                continue
            # SDK subtype taxonomy (verified against
            # claude_agent_sdk types.ResultMessage — `subtype: str`):
            #   "success" + structured_output → validated instance
            #   "error_max_structured_output_retries" → SDK gave up
            #   anything else → keep looping; the SDK may emit more results
            if message.subtype == "success" and message.structured_output is not None:
                validated = output_model.model_validate(message.structured_output)
                usage = _build_usage_info(message)
                return validated, usage
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


def _build_usage_info(message: ResultMessage) -> UsageInfo:
    """Construct a :class:`UsageInfo` from a successful ``ResultMessage``.

    foreman#227: the SDK's ``ResultMessage.usage`` is a free-form
    ``dict[str, Any] | None`` carrying the Anthropic API's usage shape
    (``input_tokens`` / ``output_tokens`` / cache-related counters).
    We read the two fields we care about with ``.get(..., 0)`` defaults
    so a partial / unexpected SDK shape doesn't crash the role runner —
    losing one stats field is strictly better than losing the whole
    run. Same defensiveness for the wall-clock counters at the
    envelope level: they're typed ``int`` on the dataclass but absent
    in some degenerate ``is_error=True`` paths.
    """
    usage_dict = message.usage or {}
    return UsageInfo(
        input_tokens=int(usage_dict.get("input_tokens", 0) or 0),
        output_tokens=int(usage_dict.get("output_tokens", 0) or 0),
        total_cost_usd=message.total_cost_usd,
        model_usage=message.model_usage,
        duration_ms=int(message.duration_ms or 0),
        duration_api_ms=int(message.duration_api_ms or 0),
        num_turns=int(message.num_turns or 0),
    )
