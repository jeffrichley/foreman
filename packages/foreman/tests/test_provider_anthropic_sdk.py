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
    UsageInfo,
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
    duration_ms: int = 0,
    duration_api_ms: int = 0,
    num_turns: int = 0,
    total_cost_usd: float | None = None,
    usage: dict[str, Any] | None = None,
    model_usage: dict[str, Any] | None = None,
) -> ResultMessage:
    """Build a minimal valid ``ResultMessage`` for the test stub.

    Mirrors the real SDK dataclass shape — the production parser fills
    these same fields; faking ones the production code reads keeps the
    test grounded. The usage / cost / duration / model_usage kwargs
    were added for foreman#227's UsageInfo extraction tests; older
    tests can leave them at the zeroed defaults.
    """
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
    result, usage = await provider.run_agent(
        system_prompt="sys",
        user_prompt="usr",
        allowed_tools=["Read"],
        output_model=_DemoOutput,
        cwd=tmp_path,
    )

    assert isinstance(result, _DemoOutput)
    assert result.name == "ok"
    assert result.count == 3
    # foreman#227: provider returns a UsageInfo alongside the output.
    assert isinstance(usage, UsageInfo)

    # The schema passed to the SDK came from the Pydantic model
    options = calls[0]["options"]
    assert options.output_format == {
        "type": "json_schema",
        "schema": _DemoOutput.model_json_schema(),
    }


# ----------------------------------------------------------------------
# Crash-recovery resume arm: session_id + resume forwarding
# ----------------------------------------------------------------------
#
# ``session_id`` (--session-id, names a new session) and ``resume`` (--resume,
# resumes an existing one) are MUTUALLY EXCLUSIVE at the claude CLI — passing
# both is rejected unless --fork-session is also given, and forking would spawn
# a NEW session, defeating resume. So the provider emits exactly one: on
# ``resume=True`` it sets ONLY ``ClaudeAgentOptions.resume`` (= session_id) and
# leaves ``session_id`` unset; otherwise it sets ONLY ``session_id``. The
# deterministic uuid5 still links the runs: fresh names the session, resume
# replays the same id. See foreman#448 (a CLI bump began enforcing this).
# Behavior is unchanged when both are left at their defaults — covered by every
# other test in this file, which asserts session_id/resume stay None.


@pytest.mark.asyncio
async def test_run_agent_resume_emits_resume_only_not_session_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On ``resume=True`` the options carry ONLY ``resume`` (the session id);
    ``session_id`` is left unset so the SDK emits ``--resume <id>`` alone and
    never the rejected ``--session-id <id> --resume <id>`` combination
    (foreman#448)."""
    calls = _patch_query(
        monkeypatch,
        [_make_result(subtype="success", structured_output={"name": "ok", "count": 1})],
    )

    provider = AnthropicSDKProvider()
    await provider.run_agent(
        system_prompt="sys",
        user_prompt="usr",
        allowed_tools=["Read"],
        output_model=_DemoOutput,
        cwd=tmp_path,
        session_id="s1",
        resume=True,
    )

    options = calls[0]["options"]
    assert options.resume == "s1"
    assert options.session_id is None


@pytest.mark.asyncio
async def test_run_agent_session_id_without_resume_pins_but_does_not_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``session_id`` alone pins the session id but leaves ``resume``
    unset (None) — the SDK starts a fresh session under that id."""
    calls = _patch_query(
        monkeypatch,
        [_make_result(subtype="success", structured_output={"name": "ok", "count": 1})],
    )

    provider = AnthropicSDKProvider()
    await provider.run_agent(
        system_prompt="sys",
        user_prompt="usr",
        allowed_tools=["Read"],
        output_model=_DemoOutput,
        cwd=tmp_path,
        session_id="s1",
    )

    options = calls[0]["options"]
    assert options.session_id == "s1"
    assert options.resume is None


@pytest.mark.asyncio
async def test_run_agent_defaults_leave_session_id_and_resume_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The inert default: no ``session_id`` / ``resume`` passed → the
    SDK options carry None for both (SDK generates a fresh session)."""
    calls = _patch_query(
        monkeypatch,
        [_make_result(subtype="success", structured_output={"name": "ok", "count": 1})],
    )

    provider = AnthropicSDKProvider()
    await provider.run_agent(
        system_prompt="sys",
        user_prompt="usr",
        allowed_tools=["Read"],
        output_model=_DemoOutput,
        cwd=tmp_path,
    )

    options = calls[0]["options"]
    assert options.session_id is None
    assert options.resume is None


@pytest.mark.asyncio
async def test_run_agent_resume_missing_session_falls_back_to_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """foreman#461: a resume into a session that was never flushed to disk
    (daemon killed before the role's transcript landed) makes the claude CLI
    print 'No conversation found with session ID: <id>' to stderr; the SDK
    surfaces it as ProcessError. The provider must retry ONCE fresh (drop
    --resume, keep the same session id) rather than surfacing
    ProviderUnknownError → NeedsHelp. A fresh session is always safe."""
    from claude_agent_sdk._errors import ProcessError

    seen_options: list[Any] = []

    def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        seen_options.append(options)

        async def gen() -> AsyncIterator[Any]:
            if options.resume is not None:
                # First attempt: resume into the missing session → hard-fail.
                raise ProcessError(
                    "Command failed",
                    exit_code=1,
                    stderr="No conversation found with session ID: s1",
                )
            # Retry: fresh session succeeds.
            yield _make_result(
                subtype="success", structured_output={"name": "ok", "count": 1}
            )

        return gen()

    monkeypatch.setattr(anthropic_sdk_module, "query", fake_query)

    provider = AnthropicSDKProvider()
    result, _usage = await provider.run_agent(
        system_prompt="sys",
        user_prompt="usr",
        allowed_tools=["Read"],
        output_model=_DemoOutput,
        cwd=tmp_path,
        session_id="s1",
        resume=True,
    )

    assert isinstance(result, _DemoOutput)
    assert result.name == "ok"
    # Exactly two SDK calls: the failed resume, then the fresh retry.
    assert len(seen_options) == 2
    assert seen_options[0].resume == "s1"  # first attempt resumes
    assert seen_options[1].resume is None  # retry is fresh…
    assert seen_options[1].session_id == "s1"  # …but reuses the session id


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
    result, _usage = await provider.run_agent(
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
    # foreman#266: unrecognised SDK exceptions translate to
    # ``ProviderUnknownError`` at the boundary. The original
    # ``RuntimeError`` is preserved as ``__cause__``.
    from foreman.providers.exceptions import ProviderUnknownError

    with pytest.raises(ProviderUnknownError, match="simulated SDK transport failure") as exc_info:
        await provider.run_agent(
            system_prompt="anything",
            user_prompt="usr",
            allowed_tools=["Read"],
            output_model=_DemoOutput,
            cwd=tmp_path,
        )

    assert isinstance(exc_info.value.__cause__, RuntimeError)
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
    result, _usage = await provider.run_agent(
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
    # foreman#266: non-auth SDK exceptions translate at the boundary to
    # ``ProviderUnknownError`` (no auth-retry, no recovery — straight
    # translation). The original ``RuntimeError`` is preserved as
    # ``__cause__``.
    from foreman.providers.exceptions import ProviderUnknownError

    with pytest.raises(ProviderUnknownError, match="not an auth error") as exc_info:
        await provider.run_agent(
            system_prompt="sys",
            user_prompt="usr",
            allowed_tools=["Read"],
            output_model=_DemoOutput,
            cwd=tmp_path,
        )

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert len(calls) == 1, "no retry should have happened for non-auth exception"
    assert not refresh_called, "refresh should not be attempted for non-auth exception"


# ----------------------------------------------------------------------
# foreman#227: per-call token usage + cost extraction from ResultMessage
# ----------------------------------------------------------------------
#
# The Claude Agent SDK's ResultMessage carries usage / total_cost_usd /
# model_usage / duration_ms / duration_api_ms / num_turns. Before this
# ticket those fields were silently dropped on the success path. The
# provider now constructs a ``UsageInfo`` from them and returns it
# alongside the validated output. These tests pin two paths:
#
#   1. Fully-populated ResultMessage → every field forwarded verbatim.
#   2. ``usage is None`` (degenerate transport / partial failure) →
#      int fields default to 0, ``total_cost_usd`` stays None, no crash.


@pytest.mark.asyncio
async def test_run_agent_forwards_usage_info_from_populated_result_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Happy path: the SDK reports a fully-populated ResultMessage. The
    provider extracts every usage field into a UsageInfo and returns
    it alongside the validated output. No round-trip data loss."""
    model_usage_payload = {
        "claude-sonnet-4-5": {
            "input_tokens": 12_345,
            "output_tokens": 678,
            "cost_usd": 0.0421,
        }
    }
    _patch_query(
        monkeypatch,
        [
            _make_result(
                subtype="success",
                structured_output={"name": "ok", "count": 1},
                duration_ms=8_421,
                duration_api_ms=7_900,
                num_turns=5,
                total_cost_usd=0.0421,
                usage={"input_tokens": 12_345, "output_tokens": 678},
                model_usage=model_usage_payload,
            )
        ],
    )

    provider = AnthropicSDKProvider()
    _result, usage = await provider.run_agent(
        system_prompt="sys",
        user_prompt="usr",
        allowed_tools=["Read"],
        output_model=_DemoOutput,
        cwd=tmp_path,
    )

    assert isinstance(usage, UsageInfo)
    assert usage.input_tokens == 12_345
    assert usage.output_tokens == 678
    assert usage.total_cost_usd == pytest.approx(0.0421)
    assert usage.model_usage == model_usage_payload
    assert usage.duration_ms == 8_421
    assert usage.duration_api_ms == 7_900
    assert usage.num_turns == 5


@pytest.mark.asyncio
async def test_run_agent_defaults_usage_info_when_result_message_usage_is_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defensive path: ``ResultMessage.usage`` is None (degenerate SDK
    state). The provider must NOT crash — it constructs a UsageInfo
    with int fields defaulting to 0 and ``total_cost_usd`` left as
    None. ``model_usage`` also passes through as None."""
    _patch_query(
        monkeypatch,
        [
            _make_result(
                subtype="success",
                structured_output={"name": "ok", "count": 1},
                duration_ms=0,
                duration_api_ms=0,
                num_turns=0,
                total_cost_usd=None,
                usage=None,
                model_usage=None,
            )
        ],
    )

    provider = AnthropicSDKProvider()
    result, usage = await provider.run_agent(
        system_prompt="sys",
        user_prompt="usr",
        allowed_tools=["Read"],
        output_model=_DemoOutput,
        cwd=tmp_path,
    )

    # Output still validates and returns normally.
    assert isinstance(result, _DemoOutput)
    # Usage falls back to zeroed defaults — no crash, no fabricated
    # values, no swallowed exception.
    assert isinstance(usage, UsageInfo)
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.total_cost_usd is None
    assert usage.model_usage is None
    assert usage.duration_ms == 0
    assert usage.duration_api_ms == 0
    assert usage.num_turns == 0


# ----------------------------------------------------------------------
# foreman#244: prompt-cache token fields extraction from ResultMessage
# ----------------------------------------------------------------------
#
# The Anthropic Agent SDK's ``ResultMessage.usage`` also carries
# ``cache_creation_input_tokens`` and ``cache_read_input_tokens`` —
# prompt-cache counters billed at 25% and 10% of the regular input
# rate. They're absent only on the first turn of a fresh agent loop;
# every subsequent turn of a multi-turn agent run (Planner / Reviewer /
# Worker / Fixer all spend most of their turns mid-loop) populates
# them. Pre-#244 we silently dropped both, so per-token columns in
# the JSONL undercounted whenever prompt caching kicked in. The SDK's
# ``total_cost_usd`` was still correct (Anthropic computes it
# server-side), but the per-token columns drifted from the cost.
#
# These tests pin two paths:
#
#   1. SDK reports both cache fields → UsageInfo forwards them verbatim.
#   2. SDK omits both cache fields (older API version, first turn of a
#      fresh loop, etc.) → UsageInfo defaults them to 0.


@pytest.mark.asyncio
async def test_run_agent_forwards_cache_tokens_from_populated_result_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Happy path: the SDK reports both cache_creation_input_tokens and
    cache_read_input_tokens. The provider extracts both into UsageInfo
    so the JSONL row carries the full prompt-cache breakdown."""
    _patch_query(
        monkeypatch,
        [
            _make_result(
                subtype="success",
                structured_output={"name": "ok", "count": 1},
                duration_ms=1_000,
                duration_api_ms=900,
                num_turns=3,
                total_cost_usd=0.012,
                usage={
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 4_096,
                    "cache_read_input_tokens": 2_048,
                },
            )
        ],
    )

    provider = AnthropicSDKProvider()
    _result, usage = await provider.run_agent(
        system_prompt="sys",
        user_prompt="usr",
        allowed_tools=["Read"],
        output_model=_DemoOutput,
        cwd=tmp_path,
    )

    assert isinstance(usage, UsageInfo)
    # Both regular fields still extracted.
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    # Prompt-cache fields populated from the SDK.
    assert usage.cache_creation_input_tokens == 4_096
    assert usage.cache_read_input_tokens == 2_048


@pytest.mark.asyncio
async def test_run_agent_defaults_cache_tokens_to_zero_when_sdk_omits_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defensive path: the SDK's usage dict omits cache_creation_input_tokens
    and cache_read_input_tokens entirely (older API version, first turn
    of a fresh agent loop, partial transport failure). UsageInfo must
    default both to 0 — no crash, no fabricated values, no swallowed
    exception."""
    _patch_query(
        monkeypatch,
        [
            _make_result(
                subtype="success",
                structured_output={"name": "ok", "count": 1},
                duration_ms=500,
                duration_api_ms=400,
                num_turns=1,
                total_cost_usd=0.001,
                usage={"input_tokens": 100, "output_tokens": 50},
            )
        ],
    )

    provider = AnthropicSDKProvider()
    _result, usage = await provider.run_agent(
        system_prompt="sys",
        user_prompt="usr",
        allowed_tools=["Read"],
        output_model=_DemoOutput,
        cwd=tmp_path,
    )

    assert isinstance(usage, UsageInfo)
    # Regular fields unaffected.
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    # Cache fields default cleanly to 0.
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_read_input_tokens == 0
