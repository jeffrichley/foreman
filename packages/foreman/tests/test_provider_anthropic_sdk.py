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
    ProviderAuthError,
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


# ----------------------------------------------------------------------
# System-prompt-via-file (Windows command-line workaround)
#
# The SDK's subprocess transport passes ``--system-prompt <text>`` as a
# command-line argument. Windows' ``CreateProcess`` caps the command line
# at 8191 chars; the Worker role's vendored-superpowers prompt sails past
# that. The provider routes through ``--system-prompt-file <path>`` by
# passing ``system_prompt={"type": "file", "path": ...}``. Foreman owns
# the file lifecycle: write before query, delete in finally. See
# foreman#50.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_routes_system_prompt_through_file_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The system prompt is materialised to a file, passed by path, and
    the file is deleted once the call returns. This is what dodges the
    Windows 8191-char command-line cap."""
    prompt_text = "huge system prompt — " * 1000  # ~20KB

    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        # The SDK gets a dict with a path, not the prompt text.
        sp = options.system_prompt
        assert isinstance(sp, dict)
        assert sp["type"] == "file"
        path = Path(sp["path"])
        # While we're inside the with-block, the file MUST exist and
        # contain the full prompt verbatim — that's what claude.exe will
        # read.
        captured["path"] = path
        captured["existed_during_call"] = path.exists()
        captured["content"] = path.read_text(encoding="utf-8")

        async def gen() -> AsyncIterator[Any]:
            yield _make_result(subtype="success", structured_output={"name": "ok", "count": 1})

        return gen()

    monkeypatch.setattr(anthropic_sdk_module, "query", fake_query)

    provider = AnthropicSDKProvider()
    result = await provider.run_agent(
        system_prompt=prompt_text,
        user_prompt="usr",
        allowed_tools=["Read"],
        output_model=_DemoOutput,
        cwd=tmp_path,
    )

    assert isinstance(result, _DemoOutput)
    assert captured["existed_during_call"] is True
    assert captured["content"] == prompt_text
    # Cleanup ran: the file is gone now that run_agent returned.
    assert not captured["path"].exists()


@pytest.mark.asyncio
async def test_run_agent_cleans_up_prompt_file_when_query_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cleanup MUST happen on the exception path too — otherwise every
    crash leaks a prompt file under ``~/.foreman/prompts/``. The
    try/finally inside the contextmanager is what guarantees this; this
    test pins it down so a future refactor (e.g. switching to a manual
    try/except) can't silently regress."""
    captured_path: list[Path] = []

    def fake_query_raises(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        sp = options.system_prompt
        assert isinstance(sp, dict)
        captured_path.append(Path(sp["path"]))

        async def gen() -> AsyncIterator[Any]:
            raise RuntimeError("simulated SDK transport failure")
            yield  # pragma: no cover — unreachable, present so this is an async generator

        return gen()

    monkeypatch.setattr(anthropic_sdk_module, "query", fake_query_raises)

    provider = AnthropicSDKProvider()
    with pytest.raises(RuntimeError, match="simulated SDK transport failure"):
        await provider.run_agent(
            system_prompt="anything",
            user_prompt="usr",
            allowed_tools=["Read"],
            output_model=_DemoOutput,
            cwd=tmp_path,
        )

    assert captured_path, "fake_query never ran — wiring is broken, not just cleanup"
    assert not captured_path[0].exists(), (
        "prompt file leaked after SDK transport raised — try/finally regressed"
    )


# ----------------------------------------------------------------------
# foreman#227: SDK auth-failure retry-once with credential refresh
# ----------------------------------------------------------------------
#
# When the container's local Claude credentials file goes stale (the
# entrypoint's periodic refresh loop hasn't caught it yet), the SDK
# raises a generic Exception whose message starts with
# ``"Claude Code returned an error result"`` (the underlying API error
# is a 401 auth failure; the SDK substitutes the protocol ``subtype``
# field for the missing ``errors`` field, producing the absurd
# ``"...error result: success"`` string).
#
# The provider catches this specific exception, attempts one credential
# refresh from the live Compose-mounted secret, and retries the query
# once. If the second attempt also auth-fails, ``ProviderAuthError`` is
# raised so the role runner can map to ``foreman:needs-help``. Non-auth
# exceptions propagate unchanged with no retry.


_AUTH_ERROR_MSG = "Claude Code returned an error result: success"


def _patch_query_sequence(
    monkeypatch: pytest.MonkeyPatch, behaviors: list[Any]
) -> list[dict[str, Any]]:
    """Make successive calls to ``query`` behave differently.

    ``behaviors`` is a list whose Nth element drives the Nth call:
        - An ``Exception`` instance → that exception is raised inside the
          async iterator.
        - A list of messages → the iterator yields them in order.

    Returns the same calls list as ``_patch_query`` so tests can assert
    how many times ``query`` was invoked.
    """
    calls: list[dict[str, Any]] = []

    def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        idx = len(calls)
        calls.append({"prompt": prompt, "options": options})
        behavior = behaviors[idx] if idx < len(behaviors) else behaviors[-1]

        async def gen() -> AsyncIterator[Any]:
            if isinstance(behavior, Exception):
                raise behavior
                yield  # pragma: no cover — unreachable
            for m in behavior:
                yield m

        return gen()

    monkeypatch.setattr(anthropic_sdk_module, "query", fake_query)
    return calls


@pytest.mark.asyncio
async def test_run_agent_retries_once_on_sdk_auth_pattern_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First call raises the SDK auth-pattern Exception; refresh attempt
    + retry succeed. The provider returns the validated Pydantic instance
    from the second attempt's successful result."""
    refresh_attempts: list[bool] = []

    def fake_refresh() -> bool:
        refresh_attempts.append(True)
        return False  # paths don't exist in the test env; that's fine

    monkeypatch.setattr(
        anthropic_sdk_module,
        "_maybe_refresh_container_creds",
        fake_refresh,
    )

    calls = _patch_query_sequence(
        monkeypatch,
        [
            Exception(_AUTH_ERROR_MSG),
            [_make_result(subtype="success", structured_output={"name": "ok", "count": 7})],
        ],
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
    assert result.count == 7
    assert len(calls) == 2, "expected exactly one retry after auth-pattern catch"
    assert len(refresh_attempts) == 1, "expected exactly one refresh attempt"


@pytest.mark.asyncio
async def test_run_agent_raises_provider_auth_error_when_retry_also_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both attempts hit the SDK auth-pattern Exception. After the
    refresh + retry, the provider gives up and raises ``ProviderAuthError``
    rather than propagating the raw generic Exception (which the
    dispatcher's runaway-loop bug would have spammed)."""
    monkeypatch.setattr(
        anthropic_sdk_module,
        "_maybe_refresh_container_creds",
        lambda: False,
    )

    calls = _patch_query_sequence(
        monkeypatch,
        [
            Exception(_AUTH_ERROR_MSG),
            Exception(_AUTH_ERROR_MSG),
        ],
    )

    provider = AnthropicSDKProvider()
    with pytest.raises(ProviderAuthError, match="failed authentication twice"):
        await provider.run_agent(
            system_prompt="sys",
            user_prompt="usr",
            allowed_tools=["Read"],
            output_model=_DemoOutput,
            cwd=tmp_path,
        )

    assert len(calls) == 2, "expected exactly one retry, no third attempt"


@pytest.mark.asyncio
async def test_run_agent_does_not_retry_on_non_auth_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-auth-pattern Exception propagates unchanged — no refresh,
    no retry. This keeps the retry guard tight: arbitrary SDK failures
    are not silently retried (which could mask real bugs or waste
    tokens)."""
    refresh_called = False

    def fake_refresh() -> bool:
        nonlocal refresh_called
        refresh_called = True
        return False

    monkeypatch.setattr(
        anthropic_sdk_module,
        "_maybe_refresh_container_creds",
        fake_refresh,
    )

    calls = _patch_query_sequence(
        monkeypatch,
        [RuntimeError("not an auth error — totally different failure")],
    )

    provider = AnthropicSDKProvider()
    with pytest.raises(RuntimeError, match="not an auth error"):
        await provider.run_agent(
            system_prompt="sys",
            user_prompt="usr",
            allowed_tools=["Read"],
            output_model=_DemoOutput,
            cwd=tmp_path,
        )

    assert len(calls) == 1, "no retry should have happened for non-auth exception"
    assert not refresh_called, "refresh should not be attempted for non-auth exception"
