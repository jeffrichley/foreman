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
    ProviderTransientError,
    ProviderUnknownError,
)
from foreman.providers.recovery import PartialResult, RecoveryChain

# foreman#361: best-effort isinstance check against the live anthropic
# SDK exception types. Guarded so test stub environments without
# ``anthropic`` installed don't break import; in that case the
# isinstance branch is a no-op and only the string-pattern path is
# active in :func:`_is_transient_sdk_error`.
try:  # pragma: no cover - import guard
    import anthropic as _anthropic  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - import guard
    _anthropic = None

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


# foreman#361: substrings the transient-classifier looks for in the
# Claude-Code-wrapped error string. One module-level tuple so future
# operators tuning the classifier have exactly one place to edit
# (Approach: "make the right thing easy"). Membership check
# lowercases the haystack before matching, so the patterns here are
# already lowercased.
_TRANSIENT_PATTERNS: tuple[str, ...] = (
    "429",
    "rate_limit",
    "rate limit",
    "overloaded",
    "503",
    "502",
    "504",
    "500",
    "connection refused",
    "connection reset",
    "connection aborted",
    "timeout",
)


# foreman#461: substrings that signal a resume into a session that isn't on
# disk. The claude CLI prints "No conversation found with session ID: <id>"
# to stderr; ProcessError surfaces it in str(exc) (message + "\nError output:
# <stderr>"). One module-level tuple so the signature has a single edit point,
# mirroring _TRANSIENT_PATTERNS. Patterns are matched against a lowercased
# haystack, so keep them lowercase.
_MISSING_SESSION_PATTERNS: tuple[str, ...] = (
    "no conversation found",
)


def _is_missing_session_sdk_error(exc: BaseException) -> bool:
    """Classify ``exc`` as a resume-into-a-session-that-isn't-on-disk failure.

    foreman#461: a daemon hard-killed before the role's Claude transcript
    flushed to the mounted ``CLAUDE_CONFIG_DIR`` leaves the named session
    unwritten, so a later ``--resume <id>`` hard-fails. The safe response is to
    retry FRESH (Stage-1 idempotency makes the redo harmless). Only meaningful
    on a ``resume=True`` dispatch — a fresh run can never raise this.
    """
    haystack = str(exc).lower()
    return any(p in haystack for p in _MISSING_SESSION_PATTERNS)


def _is_transient_sdk_error(exc: BaseException) -> bool:
    """Classify ``exc`` as an Anthropic-side transient transport failure.

    foreman#361 single source of truth for the transient classifier.
    Called from BOTH :func:`_translate_sdk_exception` (defense in
    depth for translator-only paths) AND the auth-retry guards in
    :meth:`AnthropicSDKProvider.run_agent` (the operational
    short-circuit path for Claude-Code-wrapped 5xx/429). Sharing one
    helper avoids drift between the two sites.

    The constant :data:`_SDK_AUTH_ERROR_PREFIX` is misleadingly named —
    it covers both auth and transport failures now (a Claude-Code-
    wrapped 5xx string also starts with that prefix). Renaming is
    out of scope for foreman#361 because three call sites
    (lines 167 / 272 / 299, pre-#361 numbering) reference it by
    name; a rename would balloon the diff without semantic gain.
    The dual role is documented here so the next reader knows
    what's going on.

    Returns ``True`` iff either:

    * ``exc`` is an :class:`Exception` whose ``str(exc)`` starts
      with :data:`_SDK_AUTH_ERROR_PREFIX` (case-sensitive prefix
      check) AND ``str(exc).lower()`` contains any pattern from
      :data:`_TRANSIENT_PATTERNS` (lowercased substring search).
    * OR (best-effort) ``exc`` is an instance of one of the live
      Anthropic SDK transient types
      (:class:`anthropic.APIConnectionError`,
      :class:`anthropic.RateLimitError`,
      :class:`anthropic.APITimeoutError`,
      :class:`anthropic.InternalServerError`). Guarded by the
      module-level ``try: import anthropic`` — when the SDK isn't
      installed (test stubs), only the string-pattern path is
      active.
    """
    if isinstance(exc, Exception):
        message = str(exc)
        if message.startswith(_SDK_AUTH_ERROR_PREFIX):
            haystack = message.lower()
            for needle in _TRANSIENT_PATTERNS:
                if needle in haystack:
                    return True
    if _anthropic is not None:
        sdk_transient_types: tuple[type, ...] = tuple(
            t
            for t in (
                getattr(_anthropic, "APIConnectionError", None),
                getattr(_anthropic, "RateLimitError", None),
                getattr(_anthropic, "APITimeoutError", None),
                getattr(_anthropic, "InternalServerError", None),
            )
            if t is not None
        )
        if sdk_transient_types and isinstance(exc, sdk_transient_types):
            return True
    return False


def _translate_sdk_exception(exc: BaseException) -> ProviderError:
    """Translate an SDK-level exception into the corresponding domain
    :class:`ProviderError` subclass.

    foreman#266: the boundary between the SDK and the rest of foreman
    lives here. Role runners catch the ``ProviderError`` family; SDK
    types do not leak past this function.

    Branch order (documented so the foreman#361 transient pre-check
    in :meth:`AnthropicSDKProvider.run_agent` and this translator
    stay aligned):

    1. :class:`asyncio.TimeoutError` (or subclass) →
       :class:`ProviderTimeoutError` (existing behavior).
    2. ``_is_transient_sdk_error(exc)`` →
       :class:`ProviderTransientError` (foreman#361 — covers the
       case where a transient slips past the ``run_agent``-level
       pre-check, e.g. an ``_iterate_query``-internal raise that
       re-enters translation without going through the auth-retry
       block).
    3. ``str(exc).startswith(_SDK_AUTH_ERROR_PREFIX)`` →
       :class:`ProviderAuthError` (existing — foreman#227
       auth-failure shape).
    4. Default → :class:`ProviderUnknownError(str(exc))`.

    The caller re-raises the returned exception via
    ``raise translated from exc`` so the original SDK exception is
    preserved as ``__cause__``.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return ProviderTimeoutError(str(exc) or "provider transport timed out")
    if _is_transient_sdk_error(exc):
        return ProviderTransientError(str(exc))
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
        session_id: str | None = None,
        resume: bool = False,
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
            # Crash-recovery resume arm. ``session_id`` and ``resume`` are
            # MUTUALLY EXCLUSIVE at the claude CLI: ``--session-id <id>`` names
            # a new session, ``--resume <id>`` resumes an existing one, and the
            # CLI rejects both together ("--session-id can only be used with
            # --continue or --resume if --fork-session is also specified") —
            # and ``--fork-session`` would fork into a NEW session, defeating
            # the point of resume. So the resume path emits ONLY ``--resume
            # <id>``; the fresh path emits ONLY ``--session-id <id>``. The
            # deterministic uuid5 still ties the two runs together: the fresh
            # dispatch names the session with it, and a later resume passes the
            # same id to ``--resume`` to continue that exact session. See
            # foreman#448 (a CLI bump began enforcing this; emitting both
            # silently broke every crash-recovery resume).
            if resume:
                options_kwargs["resume"] = session_id
            elif session_id is not None:
                options_kwargs["session_id"] = session_id
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
            # Control-flow order (documented as the load-bearing
            # invariant for the exception-handling block below):
            #
            #     recovery chain  →  transient pre-check (foreman#361)
            #                     →  auth-retry  →  translator
            #
            # foreman#266: the recovery chain runs FIRST so the
            # foreman#230 ``Exception("success")`` shape gets a proper
            # recovery before the auth-retry branch even considers
            # re-running the query. The two defenses target different
            # bug shapes (foreman#230 is bare ``Exception("success")``
            # with a valid ResultMessage already captured; foreman#227
            # is ``Exception("Claude Code returned an error result:
            # ...")`` with no recoverable payload) — they do not
            # overlap in practice.
            #
            # foreman#361: the transient pre-check sits BEFORE the
            # auth-retry rather than after for a load-bearing reason.
            # A Claude-Code-wrapped 503 / 429 looks IDENTICAL to an
            # auth error at the prefix level — both start with
            # ``_SDK_AUTH_ERROR_PREFIX``. Without the pre-check the
            # auth-retry would (a) needlessly refresh credentials,
            # (b) re-run the entire query while Anthropic is already
            # transient-failing — burning wall-clock + Anthropic
            # budget for nothing, (c) ultimately raise
            # ``ProviderAuthError`` and mis-route the failure to
            # NeedsHelp instead of the backoff scheduler in the
            # state machine. Pre-checking transients keeps the
            # auth-retry path meaning exactly what it says: "this
            # exception is plausibly an auth issue, retry once after
            # refreshing creds".
            #
            # Translation runs LAST so any surviving SDK exception
            # surfaces as a typed ProviderError at the boundary;
            # ``_translate_sdk_exception`` includes its own transient
            # branch as defense in depth for ``_iterate_query``-
            # internal raises that bypass the outer ``except``.
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
                # foreman#461: a resume into a session that was never flushed
                # to disk (daemon killed before the role's transcript landed)
                # hard-fails with "No conversation found". Retry ONCE FRESH —
                # drop --resume, keep the same session id so a later crash can
                # still resume THIS run — instead of surfacing
                # ProviderUnknownError → NeedsHelp. A fresh session is always
                # safe (Stage-1 idempotency). Gated on ``resume`` so a fresh
                # run can never trip it. Placed before the transient/auth
                # checks because a missing-session error is neither.
                if resume and _is_missing_session_sdk_error(e):
                    log.warning(
                        "resume session %r not on disk (killed-before-flush?); "
                        "retrying fresh (foreman#461)",
                        session_id,
                    )
                    fresh_kwargs = {
                        k: v for k, v in options_kwargs.items() if k != "resume"
                    }
                    if session_id is not None:
                        fresh_kwargs["session_id"] = session_id
                    fresh_options = ClaudeAgentOptions(**fresh_kwargs)
                    partial = PartialResult(
                        result_message=None, output_model=output_model
                    )
                    try:
                        return await self._iterate_query(
                            user_prompt=user_prompt,
                            options=fresh_options,
                            output_model=output_model,
                            partial=partial,
                        )
                    except ProviderError:
                        raise
                    except Exception as fresh_exc:
                        recovered = self._recovery.try_recover(fresh_exc, partial)
                        if recovered is not None:
                            return recovered
                        if _is_transient_sdk_error(fresh_exc):
                            raise ProviderTransientError(str(fresh_exc)) from fresh_exc
                        raise _translate_sdk_exception(fresh_exc) from fresh_exc
                # foreman#361: classify transient transport failures BEFORE
                # the auth-retry decision. A Claude-Code-wrapped 5xx/429
                # also starts with _SDK_AUTH_ERROR_PREFIX, so without this
                # guard the auth-retry block below would swallow it,
                # refresh credentials, re-run the query (burning wall-clock
                # + budget while Anthropic is already failing), and
                # ultimately mis-classify as ProviderAuthError. See the
                # recovery chain → transient pre-check → auth-retry →
                # translator comment block above.
                if _is_transient_sdk_error(e):
                    raise ProviderTransientError(str(e)) from e
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
                    # foreman#361: see the pre-retry comment above.
                    # The post-retry path also needs to classify
                    # transients BEFORE raising the "auth failed twice"
                    # ProviderAuthError.
                    if _is_transient_sdk_error(retry_exc):
                        raise ProviderTransientError(str(retry_exc)) from retry_exc
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
