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

foreman#227 (2026-06-08): ``run_agent`` returns ``tuple[T, UsageInfo]``
instead of just ``T`` so role runners can persist per-call token usage +
SDK-computed cost into their JSONL stats files. The Claude Agent SDK's
``ResultMessage`` already carries this data — we were dropping it on the
floor. This is the baseline before a second provider (Codex CLI) lands.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class UsageInfo(BaseModel):
    """Per-call token-usage + cost reported by the provider's SDK.

    Foreman role runners log these fields per JSONL stats entry so we
    can roll up cost / token consumption per role / per project / per
    issue without re-parsing in-flight state. The shape follows the
    Anthropic Agent SDK's ``ResultMessage`` directly (foreman#227); a
    future Codex provider will populate the same fields from its own
    SDK envelope.

    Defaults follow the "defensive when None" rule from the spec: if
    the SDK didn't report ``usage`` for a particular call (rare — only
    truly degenerate transport failures), ``input_tokens`` /
    ``output_tokens`` default to 0 and ``total_cost_usd`` stays
    ``None``. We never crash on a missing-usage path.

    foreman#244 added ``cache_creation_input_tokens`` /
    ``cache_read_input_tokens``. The Anthropic API charges these at
    25% / 10% of the regular input rate respectively; the SDK's
    ``ResultMessage.usage`` carries them alongside ``input_tokens`` /
    ``output_tokens``. Before #244 we silently dropped both, so any
    multi-turn agent loop (Planner / Reviewer / Worker / Fixer all
    spend most of their turns mid-loop where prompt caching is the
    default) produced JSONL rows whose per-token columns drifted from
    the SDK-computed cost. They default to 0 so callers / consumers
    don't have to special-case the first-turn / no-cache path.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    total_cost_usd: float | None = None
    model_usage: dict[str, Any] | None = None
    duration_ms: int = 0
    duration_api_ms: int = 0
    num_turns: int = 0


# Import the new domain-exception base AFTER ``UsageInfo`` is defined so
# the back-reference from ``foreman.providers.recovery`` (which imports
# ``UsageInfo`` from this module) resolves during the cycle that loading
# ``foreman.providers`` triggers — see the foreman#266 commit body for
# the full circular-import discussion.
from foreman.providers.exceptions import ProviderError  # noqa: E402


class StructuredOutputRetryError(ProviderError, RuntimeError):
    """The SDK exhausted its retry budget trying to satisfy the schema.

    Surfaced when a ``ResultMessage`` arrives with subtype
    ``error_max_structured_output_retries``. The agent produced output the
    SDK could not coerce into the supplied JSON schema after its internal
    retry budget; raising this instead of the generic "missing output"
    error gives callers a specific failure mode to handle.

    foreman#266: re-rooted under :class:`ProviderError` so role runners
    can catch the whole provider-boundary failure family with one arm.
    Multiple inheritance preserves the pre-#266 ``RuntimeError`` lineage
    so any existing ``isinstance(exc, RuntimeError)`` check still works.
    """


class StructuredOutputMissingError(ProviderError, RuntimeError):
    """No ``ResultMessage`` carrying ``structured_output`` was ever produced.

    Indicates the agent loop terminated without a successful result — either
    the transport hung up, the agent exited early, or some path the SDK
    doesn't surface as a known error subtype.

    foreman#266: re-rooted under :class:`ProviderError` — see
    :class:`StructuredOutputRetryError` for the rationale.
    """


class ProviderAuthError(ProviderError, RuntimeError):
    """The underlying provider rejected authentication.

    Surfaced after a defensive credential refresh + single retry have
    failed. The role runner should map this to a terminal blocking label
    (``foreman:needs-help``) so the dispatcher doesn't retry-spiral.
    foreman#227 (2026-06-08).

    foreman#266: re-rooted under :class:`ProviderError` — see
    :class:`StructuredOutputRetryError` for the rationale.
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
        session_id: str | None = None,
        resume: bool = False,
    ) -> tuple[T, UsageInfo]:
        """Run an agent and return ``(validated_output, usage_info)``.

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
            session_id: Optional Claude session id to pin for this run. When
                provided, it is forwarded to the SDK's ``ClaudeAgentOptions``
                so a later resume can target the same session. Default
                ``None`` — the SDK generates a fresh session id.
            resume: When True, the SDK resumes the conversation history from
                ``session_id`` (forwarded as ``claude --resume <id>``).
                Inert without a ``session_id``. Default ``False`` — start a
                new session.

        Returns:
            A tuple ``(output, usage)``. ``output`` is an instance of
            ``output_model`` validated against the agent's structured
            output. ``usage`` is a :class:`UsageInfo` carrying per-call
            token consumption + SDK-computed cost for downstream
            audit-log persistence (foreman#227). Callers MUST unpack
            both fields — the role runners append the usage fields to
            their JSONL stats entries.

        Raises:
            StructuredOutputRetryError: The SDK exhausted retries trying to
                satisfy ``output_model``'s schema.
            StructuredOutputMissingError: The agent loop completed without
                ever producing a successful ``ResultMessage`` carrying
                ``structured_output``.
        """
