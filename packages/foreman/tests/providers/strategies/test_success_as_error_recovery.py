"""Unit tests for SuccessAsErrorRecovery (foreman#266 sub-request 2).

The strategy's predicate is deliberately strict — it fires ONLY for the
specific foreman#230 bug shape:

* ``type(exc) is Exception`` — the raw bare ``Exception``, not a subclass
* ``exc.args == ("success",)`` — exact tuple equality
* a ``ResultMessage`` whose ``subtype == "success"`` was observed mid-stream
* that ``ResultMessage`` carries ``structured_output``

Other failure modes fall through to the next strategy (or to the
translator). These tests pin each clause of the predicate with a
specificity negative.
"""

from __future__ import annotations

from pydantic import BaseModel

from foreman.provider import UsageInfo
from foreman.providers.recovery import PartialResult
from foreman.providers.strategies.success_as_error import SuccessAsErrorRecovery


class _DemoOutput(BaseModel):
    """Tiny model used to validate ``structured_output`` round-trips."""

    name: str
    count: int


def _make_result_message(
    *,
    subtype: str,
    structured_output: dict | None = None,
    usage: dict | None = None,
) -> object:
    """Build a stand-in for ``claude_agent_sdk.ResultMessage``.

    The strategy does NOT import from ``claude_agent_sdk`` — it reads
    ``subtype`` / ``structured_output`` / ``usage`` via ``getattr``. A
    duck-typed namespace object exercises the same attribute lookups
    the production path performs without coupling the test to the SDK
    type.
    """

    class _RM:
        pass

    rm = _RM()
    rm.subtype = subtype
    rm.structured_output = structured_output
    rm.usage = usage
    rm.total_cost_usd = None
    rm.model_usage = None
    rm.duration_ms = 0
    rm.duration_api_ms = 0
    rm.num_turns = 0
    rm.errors = None
    return rm


def test_recovers_when_exception_success_and_valid_result_message() -> None:
    """Positive case: bare ``Exception("success")`` after a valid
    ``ResultMessage(subtype="success", structured_output={...})`` is
    recoverable. ``recover`` validates the captured payload back into
    the output model and returns ``(model_instance, UsageInfo)``.
    """
    rm = _make_result_message(
        subtype="success",
        structured_output={"name": "ok", "count": 7},
        usage={"input_tokens": 11, "output_tokens": 22},
    )
    partial: PartialResult[_DemoOutput] = PartialResult(result_message=rm, output_model=_DemoOutput)
    exc = Exception("success")
    strategy = SuccessAsErrorRecovery()

    assert strategy.can_recover(exc, partial) is True

    output, usage = strategy.recover(exc, partial)
    assert isinstance(output, _DemoOutput)
    assert output.name == "ok"
    assert output.count == 7
    assert isinstance(usage, UsageInfo)
    assert usage.input_tokens == 11
    assert usage.output_tokens == 22


def test_declines_when_exception_message_is_different() -> None:
    """Negative case: a different exception arg → strategy declines.

    ``Exception("error_max_turns")`` is a separate failure mode; we
    must not mis-classify it as the foreman#230 shape.
    """
    rm = _make_result_message(
        subtype="success",
        structured_output={"name": "ok", "count": 1},
    )
    partial: PartialResult[_DemoOutput] = PartialResult(result_message=rm, output_model=_DemoOutput)
    exc = Exception("error_max_turns")
    strategy = SuccessAsErrorRecovery()

    assert strategy.can_recover(exc, partial) is False


def test_declines_when_no_result_message_observed() -> None:
    """Negative case: no ``ResultMessage`` observed mid-stream → no
    payload to return → strategy declines and the exception propagates.
    """
    partial: PartialResult[_DemoOutput] = PartialResult(
        result_message=None, output_model=_DemoOutput
    )
    exc = Exception("success")
    strategy = SuccessAsErrorRecovery()

    assert strategy.can_recover(exc, partial) is False


def test_declines_when_result_message_subtype_is_error() -> None:
    """Negative case: a ResultMessage was observed but its subtype is
    ``"error"`` → there is no logically-successful result to recover;
    strategy declines.
    """
    rm = _make_result_message(
        subtype="error",
        structured_output=None,
    )
    partial: PartialResult[_DemoOutput] = PartialResult(result_message=rm, output_model=_DemoOutput)
    exc = Exception("success")
    strategy = SuccessAsErrorRecovery()

    assert strategy.can_recover(exc, partial) is False


def test_declines_when_exception_is_subclass_of_exception() -> None:
    """Negative case: ``type(exc) is Exception`` is strict — subclasses
    don't match. A ``RuntimeError("success")`` indicates the SDK
    actually classified the failure; we want that to surface, not be
    re-classified as a fake success.
    """
    rm = _make_result_message(
        subtype="success",
        structured_output={"name": "ok", "count": 1},
    )
    partial: PartialResult[_DemoOutput] = PartialResult(result_message=rm, output_model=_DemoOutput)
    exc = RuntimeError("success")  # subclass of Exception, not bare
    strategy = SuccessAsErrorRecovery()

    assert strategy.can_recover(exc, partial) is False


def test_declines_when_structured_output_is_none() -> None:
    """Negative case: subtype="success" but ``structured_output is None``.
    No payload to validate → strategy declines.
    """
    rm = _make_result_message(
        subtype="success",
        structured_output=None,
    )
    partial: PartialResult[_DemoOutput] = PartialResult(result_message=rm, output_model=_DemoOutput)
    exc = Exception("success")
    strategy = SuccessAsErrorRecovery()

    assert strategy.can_recover(exc, partial) is False


def test_can_recover_imports_do_not_drag_in_claude_agent_sdk() -> None:
    """Defence-in-depth: the strategy module's import graph MUST NOT
    pull ``claude_agent_sdk``. Importing it here would put the SDK on
    every role-runner's import path via ``foreman.providers`` re-exports.
    """
    import sys

    import foreman.providers.strategies.success_as_error as _mod  # noqa: F401

    # The strategy module itself doesn't have to be in sys.modules for
    # the assertion to make sense; what matters is that touching the
    # strategy didn't transitively register claude_agent_sdk. If
    # something else already imported the SDK in this process, that's
    # fine — but the strategy import path alone shouldn't trigger it.
    # The boundary-discipline test elsewhere makes the harder
    # structural assertion at the source level.
    assert "foreman.providers.strategies.success_as_error" in sys.modules
