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

import asyncio
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
from foreman.providers._usage import build_usage_info as _build_usage_info
from foreman.providers.exceptions import (
    ProviderError,
    ProviderTimeoutError,
    ProviderUnknownError,
)
from foreman.providers.recovery import PartialResult, RecoveryChain

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


def _describe_envelope(message: Any) -> str:
    """Short, log-safe description of one SDK message envelope.

    foreman#266 instrumentation: used to summarize envelopes that arrive
    AFTER the adapter has captured a satisfying ResultMessage, so we can
    learn empirically what the CLI sends between the success
    ResultMessage and either the SDK's terminal raise or a clean
    exhaust. For ``ResultMessage`` we include ``subtype`` and whether
    ``structured_output`` is present; for anything else we just include
    the class name. The result must remain short — it's emitted as part
    of a single INFO log line per call when post-success envelopes
    appear.
    """
    if isinstance(message, ResultMessage):
        subtype = getattr(message, "subtype", "?")
        has_output = getattr(message, "structured_output", None) is not None
        return f"ResultMessage(subtype={subtype}, has_output={has_output})"
    return type(message).__name__


def _translate_sdk_exception(exc: BaseException) -> ProviderError:
    """Translate an SDK-level exception into the corresponding domain
    :class:`ProviderError` subclass.

    foreman#266: the boundary between the SDK and the rest of foreman
    lives here. Role runners catch the ``ProviderError`` family; SDK
    types do not leak past this function.

    Mapping:

    * Message starts with :data:`_SDK_AUTH_ERROR_PREFIX` →
      :class:`ProviderAuthError` (the foreman#227 auth-failure shape).
    * :class:`asyncio.TimeoutError` (or any subclass) →
      :class:`ProviderTimeoutError`.
    * Anything else → :class:`ProviderUnknownError(str(exc))`.

    The caller re-raises the returned exception via
    ``raise translated from exc`` so the original SDK exception is
    preserved as ``__cause__``.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return ProviderTimeoutError(str(exc) or "provider transport timed out")
    if isinstance(exc, Exception) and str(exc).startswith(_SDK_AUTH_ERROR_PREFIX):
        return ProviderAuthError(str(exc))
    return ProviderUnknownError(str(exc))


class AnthropicSDKProvider(ProviderFacade):
    """ProviderFacade implementation backed by the Anthropic Agent SDK.

    foreman#266: accepts a :class:`RecoveryChain` of strategies that
    inspect SDK-level exceptions before the auth-retry guard fires. A
    strategy that ``can_recover`` short-circuits the exception path
    with a normal ``(validated_output, usage)`` return; unrecovered
    exceptions fall through to the existing auth-retry branch and
    then the translator.

    The constructor's ``recovery`` argument defaults to an empty chain
    so existing test fixtures that instantiate ``AnthropicSDKProvider()``
    directly continue to work. The production factory
    :func:`foreman.providers.make_provider` constructs the deployed
    chain with the registered strategies.
    """

    def __init__(self, recovery: RecoveryChain | None = None) -> None:
        self._recovery = recovery if recovery is not None else RecoveryChain([])

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

            # foreman#266: the per-call ``PartialResult`` accumulates
            # mid-iteration state (most recent ResultMessage observed)
            # so the recovery chain has something to inspect when the
            # SDK raises a known bug shape after yielding a logically
            # successful result. The same object flows through both
            # ``_iterate_query`` invocations; the second iteration is
            # not expected to fire recovery (the first one consumed
            # the stream), but it's wired uniformly so the boundary
            # discipline is the same on both paths.
            partial: PartialResult[T] = PartialResult(
                result_message=None, output_model=output_model
            )

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
            #
            # foreman#266: the recovery chain runs FIRST so the
            # foreman#230 ``Exception("success")`` shape gets a proper
            # recovery before the auth-retry branch even considers
            # re-running the query. The two defenses target different
            # bug shapes (foreman#230 is bare ``Exception("success")``
            # with a valid ResultMessage already captured; foreman#227
            # is ``Exception("Claude Code returned an error result:
            # ...")`` with no recoverable payload) — they do not
            # overlap in practice. Translation runs LAST so any
            # surviving SDK exception surfaces as a typed ProviderError
            # at the boundary.
            try:
                return await self._iterate_query(
                    user_prompt=user_prompt,
                    options=options,
                    output_model=output_model,
                    partial=partial,
                )
            except ProviderError:
                # Already a typed domain error (raised by
                # ``_iterate_query`` itself — e.g.
                # :class:`StructuredOutputRetryError`). Propagate
                # unchanged; do NOT re-translate, do NOT attempt
                # recovery (recovery is for SDK bug shapes, not for
                # explicit foreman-raised typed errors).
                raise
            except Exception as e:
                recovered = self._recovery.try_recover(e, partial)
                if recovered is not None:
                    return recovered
                if not str(e).startswith(_SDK_AUTH_ERROR_PREFIX):
                    raise _translate_sdk_exception(e) from e
                refreshed = _maybe_refresh_container_creds()
                log.warning(
                    "Caught SDK auth-error pattern (%r); refreshed=%s; retrying once",
                    str(e),
                    refreshed,
                )
                # Reset the partial so the second iteration's recovery
                # check sees fresh mid-stream state.
                partial = PartialResult(result_message=None, output_model=output_model)
                try:
                    return await self._iterate_query(
                        user_prompt=user_prompt,
                        options=options,
                        output_model=output_model,
                        partial=partial,
                    )
                except ProviderError:
                    # Same rationale as the outer ``except ProviderError``
                    # — a typed foreman error coming out of the second
                    # iteration is already classified; don't re-wrap.
                    raise
                except Exception as retry_exc:
                    recovered = self._recovery.try_recover(retry_exc, partial)
                    if recovered is not None:
                        return recovered
                    if str(retry_exc).startswith(_SDK_AUTH_ERROR_PREFIX):
                        raise ProviderAuthError(
                            "Anthropic Agent SDK failed authentication twice "
                            "in a row, including after a credentials refresh. "
                            "The container's Compose-mounted secret is likely "
                            "also stale on the host side. Original error: "
                            f"{retry_exc!r}"
                        ) from retry_exc
                    raise _translate_sdk_exception(retry_exc) from retry_exc

    async def _iterate_query(
        self,
        *,
        user_prompt: str,
        options: ClaudeAgentOptions,
        output_model: type[T],
        partial: PartialResult[T],
    ) -> tuple[T, UsageInfo]:
        """Iterate the SDK query stream and validate the structured output.

        Split out from ``run_agent`` so the auth-retry wrapper can call it
        twice. Caller is responsible for the ``ClaudeAgentOptions``
        construction + the system-prompt-file lifecycle.

        foreman#266: the caller-owned ``partial`` accumulator is
        updated as ``ResultMessage`` envelopes are seen — the recovery
        chain reads it from ``run_agent``'s exception handler when the
        SDK raises a known bug shape mid-stream.

        **Drain semantics (foreman#266 follow-up):** on observing a
        satisfying ``ResultMessage`` (``subtype="success"`` +
        ``structured_output``), the validated payload + usage are
        captured in local state but the loop continues iterating. This
        gives the SDK a chance to emit (and potentially raise on) the
        post-result envelopes that carry the foreman#230 bug — without
        draining, the consumer's early-return would throw
        ``GeneratorExit`` into the SDK at its yield point and the
        terminal raise would never fire. If the loop exits cleanly
        the captured success is returned; if the SDK raises, the
        exception propagates to the caller's recovery chain which
        re-validates from ``partial.result_message``.

        Returns ``(validated_output, usage_info)`` where ``usage_info``
        is reconstructed from the same ``ResultMessage`` that carried
        the structured output (foreman#227).
        """
        captured_success: tuple[T, UsageInfo] | None = None
        # foreman#266 instrumentation: accumulate short descriptions of
        # every envelope the SDK emits AFTER a satisfying ResultMessage
        # was captured. We have no production trace yet for what the
        # CLI sends between the success ResultMessage and either the
        # SDK's terminal raise (foreman#230 bug shape) or a clean
        # exhaust. This list feeds one log line per call that lets us
        # build an empirical model of the post-success protocol.
        # Downgrade to DEBUG (or remove) once we have a stable picture.
        post_success_envelopes: list[str] = []

        async for message in query(prompt=user_prompt, options=options):
            if captured_success is not None:
                post_success_envelopes.append(_describe_envelope(message))

            if not isinstance(message, ResultMessage):
                continue
            # foreman#266: capture the most recent ResultMessage so
            # the recovery chain has something to inspect if the SDK
            # later raises ``Exception("success")``.
            partial.result_message = message
            # SDK subtype taxonomy (verified against
            # claude_agent_sdk types.ResultMessage — `subtype: str`):
            #   "success" + structured_output → captured, keep iterating
            #   "error_max_structured_output_retries" → SDK gave up (raise)
            #   anything else → keep looping; the SDK may emit more results
            if message.subtype == "success" and message.structured_output is not None:
                # foreman#266 drain semantics: validate + capture, but
                # do NOT return yet. Continue iterating so the SDK's
                # post-result envelopes (which may include the
                # foreman#230 bug shape) get a chance to surface.
                validated = output_model.model_validate(message.structured_output)
                usage = _build_usage_info(message)
                captured_success = (validated, usage)
                continue
            if message.subtype == "error_max_structured_output_retries":
                # An explicit retry-exhausted signal after a success is
                # an SDK inconsistency we should respect rather than
                # paper over: the captured success is discarded and
                # the error surfaces. If this ever fires in production
                # with a real captured success, file a ticket so we
                # understand the SDK's intent before changing policy.
                raise StructuredOutputRetryError(
                    "Anthropic Agent SDK exhausted its retry budget trying to "
                    f"satisfy the schema for {output_model.__name__}. "
                    f"Errors reported: {message.errors!r}"
                )

        # Loop exited cleanly. If we captured a success along the way,
        # return it; otherwise the SDK produced no successful result.
        if captured_success is not None:
            if post_success_envelopes:
                # The interesting data point: the SDK kept emitting
                # envelopes after our satisfying ResultMessage and then
                # exhausted cleanly (no raise). Log so we can build an
                # empirical model of post-success protocol shape — most
                # role runs won't hit this, so the line is rare-enough
                # to be informational rather than noisy.
                log.info(
                    "provider drain: stream exhausted after captured success "
                    "with %d post-success envelope(s): %s",
                    len(post_success_envelopes),
                    post_success_envelopes,
                )
            return captured_success

        raise StructuredOutputMissingError(
            "Anthropic Agent SDK did not return a successful ResultMessage "
            f"carrying structured_output for {output_model.__name__}"
        )
