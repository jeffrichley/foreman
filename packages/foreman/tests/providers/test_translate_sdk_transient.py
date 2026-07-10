"""Tests for the foreman#361 transient-classifier branches.

Two layers:

1. ``_translate_sdk_exception`` unit tests — the translator's NEW
   ``ProviderTransientError`` branch. Defense-in-depth for paths
   that bypass the auth-retry block.
2. ``run_agent``-level integration tests — the auth-retry control-
   flow short-circuits at the two prefix-match decision points
   inside ``AnthropicSDKProvider.run_agent``. Without those
   short-circuits a Claude-Code-wrapped 5xx/429 would be swallowed
   by the credential-refresh path and mis-classified as
   ``ProviderAuthError``. These tests would FAIL against the
   unfixed control flow.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from foreman.provider import ProviderAuthError
from foreman.providers import anthropic_sdk as anthropic_sdk_module
from foreman.providers.anthropic_sdk import (
    AnthropicSDKProvider,
    _translate_sdk_exception,
)
from foreman.providers.exceptions import (
    ProviderError,
    ProviderTransientError,
    ProviderUnknownError,
)

# ---------------------------------------------------------------
# Layer 1: _translate_sdk_exception
# ---------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Claude Code returned an error result: 429 Too Many Requests",
        "Claude Code returned an error result: 503 Service Unavailable",
        "Claude Code returned an error result: 502 Bad Gateway",
        "Claude Code returned an error result: 504 Gateway Timeout",
        "Claude Code returned an error result: 500 Internal Server Error",
        "Claude Code returned an error result: rate_limit_error",
        "Claude Code returned an error result: rate limit hit",
        "Claude Code returned an error result: overloaded",
        "Claude Code returned an error result: connection refused upstream",
        "Claude Code returned an error result: connection reset by peer",
        "Claude Code returned an error result: connection aborted",
        "Claude Code returned an error result: timeout after 30s",
    ],
)
def test_translates_transient_patterns_to_provider_transient_error(
    message: str,
) -> None:
    """Strings matching the transient classifier map to
    ``ProviderTransientError``, NOT ``ProviderAuthError`` /
    ``ProviderUnknownError``.
    """
    translated = _translate_sdk_exception(Exception(message))
    assert isinstance(translated, ProviderTransientError)
    assert isinstance(translated, ProviderError)


@pytest.mark.parametrize(
    "message",
    [
        # Benign auth failure — the original foreman#227 shape.
        "Claude Code returned an error result: success",
        # Random non-wrapped failure — should remain
        # ProviderUnknownError.
        "some random failure",
        "",
    ],
)
def test_benign_strings_are_not_classified_as_transient(message: str) -> None:
    """Benign-looking strings (the foreman#227 auth-failure shape,
    random failures) do NOT map to ``ProviderTransientError``.
    """
    translated = _translate_sdk_exception(Exception(message))
    assert not isinstance(translated, ProviderTransientError)


# ---------------------------------------------------------------
# Layer 2: AnthropicSDKProvider.run_agent integration tests for the
# auth-retry control-flow short-circuit (foreman#361 Reviewer-
# identified gap). Without these short-circuits the transient
# mechanism would be dead code for the common scenario.
# ---------------------------------------------------------------


class _DemoOutput(BaseModel):
    """Minimal pydantic model to satisfy ``run_agent``'s output_model
    plumbing. The tests below raise from ``query()`` before any
    structured output is produced, so the model is only used by the
    JSON-schema dump.
    """

    name: str = ""


def _patch_query_raises_sequence(
    monkeypatch: pytest.MonkeyPatch, *, exceptions: list[Exception]
) -> None:
    """Replace ``query`` with one that raises the next exception in
    ``exceptions`` on each call. Mirrors the helper used in
    ``test_adapter_integration.py`` but iterates through a sequence
    so we can exercise both the first-call path and the auth-retry
    path within one test.
    """
    pending = list(exceptions)

    def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            raise pending.pop(0)
            yield  # pragma: no cover — keeps this an async generator

        return gen()

    monkeypatch.setattr(anthropic_sdk_module, "query", fake_query)


@pytest.mark.asyncio
async def test_run_agent_classifies_wrapped_transient_as_transient(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Claude-Code-wrapped 503 raised from the first ``_iterate_query``
    call surfaces as ``ProviderTransientError`` AND does NOT trigger
    the credential-refresh helper.
    """
    wrapped_503 = Exception("Claude Code returned an error result: 503 Service Unavailable")
    _patch_query_raises_sequence(monkeypatch, exceptions=[wrapped_503])

    refresh = MagicMock(return_value=False)
    monkeypatch.setattr(anthropic_sdk_module, "_maybe_refresh_container_creds", refresh)

    provider = AnthropicSDKProvider()
    with pytest.raises(ProviderTransientError) as exc_info:
        await provider.run_agent(
            system_prompt="sys",
            user_prompt="user",
            allowed_tools=[],
            output_model=_DemoOutput,
            cwd=tmp_path,
        )

    # The transient subclass MUST surface — auth-retry must not have
    # swallowed it.
    assert isinstance(exc_info.value.__cause__, Exception)
    assert "503" in str(exc_info.value)
    assert not isinstance(exc_info.value, ProviderAuthError)
    assert not isinstance(exc_info.value, ProviderUnknownError)
    # Critical: the credential-refresh helper MUST NOT have been
    # called. Without the foreman#361 pre-check this would be called
    # once before the auth-retry tried the query a second time.
    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_run_agent_classifies_wrapped_429_post_retry_as_transient(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A first call raises a non-transient prefix-matching exception
    (forcing the auth-retry path) and the retry call raises a
    prefix-matching 429. ``run_agent`` raises ``ProviderTransientError``
    rather than the "auth failed twice" ``ProviderAuthError``.
    """
    non_transient_auth = Exception("Claude Code returned an error result: success")
    wrapped_429 = Exception("Claude Code returned an error result: 429 Too Many Requests")
    _patch_query_raises_sequence(monkeypatch, exceptions=[non_transient_auth, wrapped_429])

    refresh = MagicMock(return_value=True)
    monkeypatch.setattr(anthropic_sdk_module, "_maybe_refresh_container_creds", refresh)

    provider = AnthropicSDKProvider()
    with pytest.raises(ProviderTransientError) as exc_info:
        await provider.run_agent(
            system_prompt="sys",
            user_prompt="user",
            allowed_tools=[],
            output_model=_DemoOutput,
            cwd=tmp_path,
        )

    assert "429" in str(exc_info.value)
    assert not isinstance(exc_info.value, ProviderAuthError)
    # The auth-retry DID run (refresh was called) because the first
    # exception was non-transient. But the second (transient) was
    # short-circuited BEFORE the "auth failed twice" branch.
    refresh.assert_called_once()
