"""Recovery for foreman#230: SDK raises ``Exception("success")`` after
yielding a valid ``ResultMessage(subtype="success")``.

Context: ``claude_agent_sdk._internal.query.receive_messages`` treats
every ``type == "error"`` raw envelope as a failure without inspecting
the protocol-level subtype. When the CLI emits a logically-successful
run as a ``type="error"`` envelope whose payload is the subtype
string ``"success"``, the SDK raises ``Exception("success")``. The
ResultMessage carrying the real structured output is yielded FIRST,
then the spurious raise happens AFTER. By the time the exception
fires, the adapter already has a valid payload — we just need to
return it.

Recovery predicate is intentionally strict (see :mod:`foreman.providers.recovery`
for the design rationale):

* ``type(exc) is Exception`` — the raw bare ``Exception``, NOT a
  subclass. A ``RuntimeError("success")`` indicates the SDK actually
  classified the failure; surface it as a real error.
* ``exc.args == ("success",)`` exactly. The foreman#227 auth-failure
  shape ``Exception("Claude Code returned an error result: success")``
  produces ``args == ("Claude Code returned an error result: success",)``
  — different shape, different defense (the auth-retry guard handles
  that one).
* A ``ResultMessage`` was observed mid-stream AND its subtype is
  ``"success"`` AND its ``structured_output`` is not ``None``. No
  recoverable payload → no recovery; the exception propagates.
"""

from __future__ import annotations

from typing import Any

from foreman.provider import UsageInfo
from foreman.providers._usage import build_usage_info
from foreman.providers.recovery import PartialResult, RecoveryStrategy


class SuccessAsErrorRecovery(RecoveryStrategy):
    """Recover from the foreman#230 bare-``Exception("success")`` shape."""

    def can_recover(self, exc: BaseException, partial: PartialResult[Any]) -> bool:
        # ``type(exc) is Exception`` — strict identity; subclasses must
        # NOT match (a classified SDK error should surface, not be
        # silently re-classified as a fake success).
        if type(exc) is not Exception:
            return False
        # ``exc.args == ("success",)`` — strict equality. A multi-arg
        # or differently-worded Exception is a different bug.
        if exc.args != ("success",):
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
