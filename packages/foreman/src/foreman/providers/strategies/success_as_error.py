r"""Recovery for the upstream ``claude_agent_sdk`` ``Exception("success")`` bug.

Handles the bug family where a valid ``ResultMessage(subtype="success")``
was already yielded before the SDK raised.

Upstream issue
--------------
``claude_agent_sdk._internal.query.receive_messages`` (line 852 at SDK
0.2.87, the version pinned at time of writing) treats every
``type == "error"`` raw envelope as a failure without inspecting the
protocol-level subtype:

.. code-block:: python

    async for message in self._message_receive:
        if message.get("type") == "end":
            break
        elif message.get("type") == "error":
            raise Exception(message.get("error", "Unknown error"))
        yield message

When the CLI emits a logically-successful run as TWO messages — first a
``type="result"`` (yielded as a valid ``ResultMessage(subtype="success",
structured_output={...})``), then a ``type="error"`` envelope whose
payload is some flavor of the literal subtype string — the SDK raises
even though the prior yield already delivered everything we need.

Upstream report + bug-introduction history
------------------------------------------
* Regression introduced in claude-agent-sdk **0.2.27** (commit
  ``0ed5eea``, 2025-01-30). Last known-good version: **0.2.25**.
* Tracking issues against the surrounding ecosystem (same underlying
  SDK bug class):

  - https://github.com/anthropics/claude-code-action/issues/892
    (\"SDK execution error in Agent SDK 0.2.27+ causes crash before
    API calls\")
  - https://github.com/anthropics/claude-code-action/issues/804
    (\"Claude Code exits with code 1 before making API calls\")
  - https://github.com/anthropics/claude-code-action/issues/893
    (\"SDK execution error\")

* The result-object shape upstream reports: ``{"type": "result",
  "subtype": "success", "is_error": true}`` — contradictory: subtype
  claims success but ``is_error`` is true. The CLI then emits a
  separate error envelope whose ``error`` field is one of two shapes:

  - **Bare**: ``"success"`` → ``Exception("success")``
  - **Wrapped**: ``"Claude Code returned an error result: success"``
    → ``Exception("Claude Code returned an error result: success")``

Both surface the same underlying bug at the SAME SDK line (``raise
Exception(message.get("error", ...))``); the prior ``yield`` of the
ResultMessage already delivered the real payload in both cases. The
original predicate (foreman#230) recovered ONLY the bare form, on the
incorrect belief that the wrapped form was an auth failure
(foreman#227) — its prefix ``\"Claude Code returned an error result\"``
matches ``_SDK_AUTH_ERROR_PREFIX``. The 2026-06-20 overnight outage
(see commit history near this change) proved the wrapped form is the
same SDK bug, not an auth issue: the auth-retry path can't recover
because there's no auth issue to fix; it fails twice and raises
``ProviderAuthError``, escalating tickets to NeedsHelp.

When this is upstream-fixed
---------------------------
If claude-agent-sdk later inspects ``subtype`` before raising (or stops
emitting the spurious error envelope entirely), this strategy becomes
dead code: ``can_recover`` will return False because the SDK no longer
raises. You can verify by removing the strategy from ``make_provider``
and running ``just check`` — if green, delete this file. The
regression test in
``tests/providers/strategies/test_success_as_error_recovery.py`` pins
the bug shape so a future SDK upgrade test reveals if upstream changed
the emission pattern.

Recovery predicate (deliberately strict — see
:mod:`foreman.providers.recovery` for the design rationale):

* ``type(exc) is Exception`` — the raw bare ``Exception``, NOT a
  subclass. A ``RuntimeError("success")`` indicates the SDK actually
  classified the failure; surface it as a real error.
* ``exc.args`` matches one of the two known upstream shapes
  (see ``_RECOVERABLE_ARGS`` below). A multi-arg or differently-worded
  Exception is a different bug.
* A ``ResultMessage`` was observed mid-stream AND its subtype is
  ``"success"`` AND its ``structured_output`` is not ``None``. No
  recoverable payload → no recovery; the exception propagates.
"""

from __future__ import annotations

from typing import Any

from foreman.provider import UsageInfo
from foreman.providers._usage import build_usage_info
from foreman.providers.recovery import PartialResult, RecoveryStrategy

#: The two known upstream exception-arg tuples that signal the same SDK
#: bug (see module docstring). Both are observed at
#: ``claude_agent_sdk._internal.query.py:852``. If a future SDK release
#: changes the emission shape and a new arg tuple appears, add it here
#: rather than spawning a sibling strategy — the recovery mechanics
#: (ResultMessage-already-yielded) are identical.
_RECOVERABLE_ARGS: frozenset[tuple[object, ...]] = frozenset(
    {
        # foreman#230 bare form (the original case the strategy shipped for).
        ("success",),
        # 2026-06-20 wrapped form (upstream SDK envelope wraps the same
        # "success" payload one layer deeper). Pinned tickets:
        # foreman#335, #363, #367 all crashed with this shape during
        # the overnight stress test.
        ("Claude Code returned an error result: success",),
    }
)


class SuccessAsErrorRecovery(RecoveryStrategy):
    """Recover from the upstream ``Exception("success")`` family.

    Both the bare and wrapped variants (see module docstring) are
    handled by re-validating the ``structured_output`` already
    captured on a pre-yielded ResultMessage, so a spurious post-success
    raise doesn't propagate as a failure.
    """

    def can_recover(self, exc: BaseException, partial: PartialResult[Any]) -> bool:
        """Return True iff ``exc`` matches the known bug shape with a usable payload.

        Requires all three of: ``exc`` is a bare ``Exception`` (not a
        subclass), its ``args`` match one of the two known upstream
        message shapes, and ``partial`` captured a ``ResultMessage``
        with ``subtype="success"`` and a non-``None``
        ``structured_output`` to recover from.
        """
        # ``type(exc) is Exception`` — strict identity; subclasses must
        # NOT match (a classified SDK error should surface, not be
        # silently re-classified as a fake success).
        if type(exc) is not Exception:
            return False
        # Match against the known upstream shapes. See module docstring
        # for the bug history and ``_RECOVERABLE_ARGS`` for the set.
        if exc.args not in _RECOVERABLE_ARGS:
            return False
        # Need a mid-stream ResultMessage to recover from. Without one,
        # there is no payload to return — the exception propagates.
        rm = partial.result_message
        if rm is None:
            return False
        # The captured ResultMessage must be a logically-successful one
        # whose structured_output is present. A subtype="error" message
        # in the partial state means the SDK already told us this run
        # failed; we should not paper over that.
        if getattr(rm, "subtype", None) != "success":
            return False
        if getattr(rm, "structured_output", None) is None:
            return False
        return True

    def recover(self, exc: BaseException, partial: PartialResult[Any]) -> tuple[Any, UsageInfo]:
        """Re-validate the captured ResultMessage's structured output and rebuild usage.

        Assumes ``can_recover`` already returned True for the same
        ``(exc, partial)`` pair, so ``partial.result_message`` and
        ``partial.output_model`` are guaranteed present; the asserts
        below are defense-in-depth for direct callers.
        """
        # ``can_recover`` returning True implies the partial state has
        # everything we need; the asserts below are defense-in-depth
        # for the case where a caller invokes ``recover`` directly
        # (the chain never does — but a direct invocation should fail
        # loudly rather than crash with AttributeError).
        rm = partial.result_message
        assert rm is not None, "recover() invoked without a captured ResultMessage"
        assert partial.output_model is not None, (
            "recover() invoked without an output_model on PartialResult"
        )
        validated = partial.output_model.model_validate(rm.structured_output)
        usage = build_usage_info(rm)
        return validated, usage
