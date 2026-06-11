"""End-to-end integration tests for the refactored adapter (foreman#266
sub-request 6).

Drives a fake SDK message stream through ``AnthropicSDKProvider`` using
the same ``_patch_query`` pattern as the existing
``test_provider_anthropic_sdk.py``. Covers the three boundary contracts:

* Normal ``subtype="success"`` path → validated tuple
* ``Exception("success")`` after a valid result → recovered via chain
* Unknown SDK exception → translated to ``ProviderUnknownError``
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ResultMessage
from pydantic import BaseModel

from foreman.provider import ProviderAuthError, UsageInfo
from foreman.providers import anthropic_sdk as anthropic_sdk_module
from foreman.providers.anthropic_sdk import AnthropicSDKProvider
from foreman.providers.exceptions import ProviderUnknownError
from foreman.providers.recovery import (
    PartialResult,
    RecoveryChain,
    RecoveryStrategy,
)
from foreman.providers.strategies.success_as_error import SuccessAsErrorRecovery


class _AlwaysRecoverStrategy(RecoveryStrategy):
    """Test-local strategy that recovers any ``Exception("success")``
    by returning a hard-coded ``(_DemoOutput, UsageInfo)`` tuple.

    Used to exercise the adapter's wiring (chain consultation in the
    exception path) without depending on the production strategy's
    strict predicate, which is unit-tested separately. The deployed
    :class:`SuccessAsErrorRecovery` requires a captured ResultMessage
    in the partial state — a precondition the success-shortcut path in
    ``_iterate_query`` would already have returned for, so it cannot
    be exercised end-to-end with the natural fake-query shape.
    """

    def __init__(self, payload: tuple[Any, UsageInfo]) -> None:
        self._payload = payload

    def can_recover(self, exc: BaseException, partial: PartialResult[Any]) -> bool:
        return isinstance(exc, Exception) and exc.args == ("success",)

    def recover(self, exc: BaseException, partial: PartialResult[Any]) -> tuple[Any, UsageInfo]:
        return self._payload


class _DemoOutput(BaseModel):
    name: str
    count: int


def _make_result(
    *,
    subtype: str,
    structured_output: dict[str, Any] | None = None,
    errors: list[str] | None = None,
    duration_ms: int = 0,
    duration_api_ms: int = 0,
    num_turns: int = 0,
    total_cost_usd: float | None = None,
    usage: dict[str, Any] | None = None,
    model_usage: dict[str, Any] | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=duration_ms,
        duration_api_ms=duration_api_ms,
        is_error=subtype != "success",
        num_turns=num_turns,
        session_id="test-session",
        structured_output=structured_output,
        errors=errors,
        total_cost_usd=total_cost_usd,
        usage=usage,
        model_usage=model_usage,
    )


def _patch_query_yielding_then_raising(
    monkeypatch: pytest.MonkeyPatch,
    *,
    messages: list[Any],
    raise_after: BaseException | None = None,
) -> None:
    """Replace ``query`` with an async iterator that yields each message
    then optionally raises ``raise_after`` (the foreman#230 shape).
    """

    def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            for m in messages:
                yield m
            if raise_after is not None:
                raise raise_after

        return gen()

    monkeypatch.setattr(anthropic_sdk_module, "query", fake_query)


def _patch_query_raising_each_call(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exc: BaseException,
) -> None:
    """Replace ``query`` with one that raises ``exc`` on each call."""

    def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            raise exc
            yield  # pragma: no cover — keeps this an async generator

        return gen()

    monkeypatch.setattr(anthropic_sdk_module, "query", fake_query)


@pytest.mark.asyncio
async def test_normal_success_path_returns_validated_tuple(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Normal subtype=success result → validated ``(model, usage)``.

    Chain includes ``SuccessAsErrorRecovery`` but it doesn't fire
    because no exception is raised — the happy path is unchanged.
    """
    _patch_query_yielding_then_raising(
        monkeypatch,
        messages=[_make_result(subtype="success", structured_output={"name": "x", "count": 3})],
    )

    provider = AnthropicSDKProvider(recovery=RecoveryChain([SuccessAsErrorRecovery()]))
    result, usage = await provider.run_agent(
        system_prompt="sys",
        user_prompt="usr",
        allowed_tools=["Read"],
        output_model=_DemoOutput,
        cwd=tmp_path,
    )

    assert isinstance(result, _DemoOutput)
    assert result.name == "x"
    assert result.count == 3
    assert isinstance(usage, UsageInfo)


@pytest.mark.asyncio
async def test_recovery_chain_handles_exception_when_iteration_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The adapter consults its recovery chain in the exception path
    BEFORE the auth-retry guard fires. When a strategy claims the
    exception, the adapter returns the strategy's payload instead of
    propagating, and emits the structured recovery log line.

    Implementation note: the production
    :class:`SuccessAsErrorRecovery` predicate requires a captured
    ``ResultMessage`` with ``subtype="success"`` + ``structured_output``
    — but those same conditions cause ``_iterate_query`` to return
    BEFORE the SDK can raise. To exercise the wiring without depending
    on a contradiction in the production strategy, we use a
    test-local strategy that recovers any
    ``Exception("success")``. The production strategy's strict
    predicate is unit-tested separately.
    """
    raise_exc = Exception("success")
    _patch_query_raising_each_call(monkeypatch, exc=raise_exc)

    payload_model = _DemoOutput(name="recovered", count=9)
    payload_usage = UsageInfo(input_tokens=1, output_tokens=2)
    provider = AnthropicSDKProvider(
        recovery=RecoveryChain([_AlwaysRecoverStrategy(payload=(payload_model, payload_usage))])
    )
    caplog.set_level(logging.INFO, logger="foreman.providers.recovery")
    result, usage = await provider.run_agent(
        system_prompt="sys",
        user_prompt="usr",
        allowed_tools=["Read"],
        output_model=_DemoOutput,
        cwd=tmp_path,
    )

    assert result is payload_model
    assert usage is payload_usage

    recovery_log_lines = [
        r.getMessage() for r in caplog.records if "provider recovery" in r.getMessage()
    ]
    assert any("_AlwaysRecoverStrategy" in line for line in recovery_log_lines), (
        f"expected a recovery log line naming _AlwaysRecoverStrategy; got {recovery_log_lines!r}"
    )


@pytest.mark.asyncio
async def test_partial_result_carries_captured_result_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sanity check: the adapter populates ``PartialResult.result_message``
    as it iterates the SDK stream — observed by a strategy that records
    what it sees on ``can_recover``.

    Why this matters: the production
    :class:`SuccessAsErrorRecovery` reads ``partial.result_message`` to
    decide whether the exception is recoverable. If the adapter ever
    stopped populating that field, the strategy would silently never
    fire. This test pins the contract.
    """
    captured_messages: list[Any] = []

    class _RecorderStrategy(RecoveryStrategy):
        def can_recover(self, exc: BaseException, partial: PartialResult[Any]) -> bool:
            captured_messages.append(partial.result_message)
            return False

        def recover(self, exc: BaseException, partial: PartialResult[Any]) -> tuple[Any, UsageInfo]:
            raise AssertionError("should not be called — can_recover returned False")

    # Yield a non-success ResultMessage so the adapter sets partial
    # but does NOT short-circuit, then raise to drive the except path.
    rm = _make_result(subtype="error_other", structured_output=None)

    def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            yield rm
            raise Exception("unrecoverable")

        return gen()

    monkeypatch.setattr(anthropic_sdk_module, "query", fake_query)

    provider = AnthropicSDKProvider(recovery=RecoveryChain([_RecorderStrategy()]))
    with pytest.raises(ProviderUnknownError):
        await provider.run_agent(
            system_prompt="sys",
            user_prompt="usr",
            allowed_tools=["Read"],
            output_model=_DemoOutput,
            cwd=tmp_path,
        )

    assert captured_messages, "strategy never saw a partial — recovery wiring is broken"
    assert captured_messages[0] is rm, (
        f"expected partial.result_message to be the yielded ResultMessage; got {captured_messages[0]!r}"
    )


@pytest.mark.asyncio
async def test_unknown_sdk_exception_translates_to_provider_unknown_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unrecognized exception (no recovery match, no auth-prefix)
    translates to ``ProviderUnknownError`` with ``__cause__`` preserved.
    """
    original = Exception("totally weird")
    _patch_query_raising_each_call(monkeypatch, exc=original)

    provider = AnthropicSDKProvider(recovery=RecoveryChain([]))
    with pytest.raises(ProviderUnknownError) as exc_info:
        await provider.run_agent(
            system_prompt="sys",
            user_prompt="usr",
            allowed_tools=["Read"],
            output_model=_DemoOutput,
            cwd=tmp_path,
        )

    assert exc_info.value.__cause__ is original
    assert "totally weird" in str(exc_info.value)


@pytest.mark.asyncio
async def test_auth_prefix_path_still_translates_to_provider_auth_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The existing foreman#227 auth-retry guard still fires for the
    auth-prefix shape. After the second failed attempt the adapter
    raises ``ProviderAuthError``.
    """
    auth_exc = Exception("Claude Code returned an error result: 401 unauth")
    _patch_query_raising_each_call(monkeypatch, exc=auth_exc)

    provider = AnthropicSDKProvider(recovery=RecoveryChain([]))
    with pytest.raises(ProviderAuthError):
        await provider.run_agent(
            system_prompt="sys",
            user_prompt="usr",
            allowed_tools=["Read"],
            output_model=_DemoOutput,
            cwd=tmp_path,
        )
