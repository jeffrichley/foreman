"""Smoke + subtype-handling tests for AnthropicSDKProvider.

Real end-to-end agent runs are gated behind the ``real_engine`` pytest
marker (per the never-done-without-running rule). This file covers
structural correctness of the adapter — including the Pydantic-first
contract and the SDK ``ResultMessage.subtype`` dispatch — without making
real API calls.

The Anthropic Agent SDK's ``ResultMessage`` carries a free-form ``subtype:
str``. The values this adapter cares about are:

* ``"success"`` — paired with ``structured_output`` → validated instance
* ``"error_max_structured_output_retries"`` — SDK exhausted its retries
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ResultMessage
from pydantic import BaseModel

from foreman.provider import (
    ProviderFacade,
    StructuredOutputMissingError,
    StructuredOutputRetryError,
)
from foreman.providers import anthropic_sdk as anthropic_sdk_module
from foreman.providers.anthropic_sdk import AnthropicSDKProvider


class _DemoOutput(BaseModel):
    """Tiny Pydantic model used for structured-output round-trip tests."""

    name: str
    count: int


def _make_result(
    *,
    subtype: str,
    structured_output: dict[str, Any] | None = None,
    errors: list[str] | None = None,
) -> ResultMessage:
    """Build a minimal valid ``ResultMessage`` for the test stub.

    Mirrors the real SDK dataclass shape — the production parser fills
    these same fields; faking ones the production code reads keeps the
    test grounded.
    """
    return ResultMessage(
        subtype=subtype,
        duration_ms=0,
        duration_api_ms=0,
        is_error=subtype != "success",
        num_turns=0,
        session_id="test-session",
        structured_output=structured_output,
        errors=errors,
    )


def _patch_query(monkeypatch: pytest.MonkeyPatch, messages: list[Any]) -> list[dict[str, Any]]:
    """Replace ``query`` with an async iterator yielding ``messages``.

    Returns a list that the stub appends each call's kwargs to, so tests
    can assert on what was passed to the SDK (schema dict, tools, etc.).
    """
    calls: list[dict[str, Any]] = []

    def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        calls.append({"prompt": prompt, "options": options})

        async def gen() -> AsyncIterator[Any]:
            for m in messages:
                yield m

        return gen()

    monkeypatch.setattr(anthropic_sdk_module, "query", fake_query)
    return calls


# ----------------------------------------------------------------------
# Type / wiring smoke tests
# ----------------------------------------------------------------------


def test_provider_can_be_instantiated() -> None:
    provider = AnthropicSDKProvider()
    assert isinstance(provider, ProviderFacade)


def test_provider_inherits_from_facade() -> None:
    assert issubclass(AnthropicSDKProvider, ProviderFacade)


# ----------------------------------------------------------------------
# Pydantic-first happy path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_returns_validated_pydantic_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The provider validates ``structured_output`` against ``output_model``
    and returns a typed instance (no dicts at the boundary)."""
    calls = _patch_query(
        monkeypatch,
        [_make_result(subtype="success", structured_output={"name": "ok", "count": 3})],
    )

    provider = AnthropicSDKProvider()
    result = await provider.run_agent(
        system_prompt="sys",
        user_prompt="usr",
        allowed_tools=["Read"],
        output_model=_DemoOutput,
        cwd=tmp_path,
    )

    assert isinstance(result, _DemoOutput)
    assert result.name == "ok"
    assert result.count == 3

    # The schema passed to the SDK came from the Pydantic model
    options = calls[0]["options"]
    assert options.output_format == {
        "type": "json_schema",
        "schema": _DemoOutput.model_json_schema(),
    }


# ----------------------------------------------------------------------
# Subtype error handling
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_raises_retry_error_on_max_structured_output_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SDK subtype ``error_max_structured_output_retries`` surfaces as a
    specific exception — not the generic missing-output error."""
    _patch_query(
        monkeypatch,
        [
            _make_result(
                subtype="error_max_structured_output_retries",
                errors=["schema mismatch on field 'name'"],
            )
        ],
    )

    provider = AnthropicSDKProvider()
    with pytest.raises(StructuredOutputRetryError, match="exhausted its retry budget"):
        await provider.run_agent(
            system_prompt="sys",
            user_prompt="usr",
            allowed_tools=["Read"],
            output_model=_DemoOutput,
            cwd=tmp_path,
        )


@pytest.mark.asyncio
async def test_run_agent_raises_missing_error_when_no_success_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ResultMessage with success+structured_output → MissingError."""
    _patch_query(monkeypatch, [])  # empty stream — no result at all

    provider = AnthropicSDKProvider()
    with pytest.raises(StructuredOutputMissingError):
        await provider.run_agent(
            system_prompt="sys",
            user_prompt="usr",
            allowed_tools=["Read"],
            output_model=_DemoOutput,
            cwd=tmp_path,
        )


@pytest.mark.asyncio
async def test_run_agent_raises_missing_error_when_success_has_no_structured_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A success ResultMessage without ``structured_output`` is treated as
    a missing-output failure (the SDK's contract is success ⇒ data)."""
    _patch_query(
        monkeypatch,
        [_make_result(subtype="success", structured_output=None)],
    )

    provider = AnthropicSDKProvider()
    with pytest.raises(StructuredOutputMissingError):
        await provider.run_agent(
            system_prompt="sys",
            user_prompt="usr",
            allowed_tools=["Read"],
            output_model=_DemoOutput,
            cwd=tmp_path,
        )
