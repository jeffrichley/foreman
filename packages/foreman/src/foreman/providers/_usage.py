"""Shared helper for reconstructing :class:`UsageInfo` from a SDK message.

Lives at the providers package level — separate from
``anthropic_sdk.py`` — so both the adapter and the recovery strategies
can read the same shape without a circular import (the strategies
must not depend on the concrete adapter module, which would drag
``claude_agent_sdk`` into the strategy import graph).

The helper reads attributes via ``getattr`` rather than ``isinstance``
checks against the SDK type so it works against both the real
``claude_agent_sdk.ResultMessage`` and the duck-typed test stubs that
the strategy-level unit tests use.
"""

from __future__ import annotations

from typing import Any

from foreman.provider import UsageInfo


def build_usage_info(message: Any) -> UsageInfo:
    """Construct a :class:`UsageInfo` from a ``ResultMessage``-shaped object.

    foreman#227: ``ResultMessage.usage`` is a free-form
    ``dict[str, Any] | None`` carrying the Anthropic API's usage shape
    (``input_tokens`` / ``output_tokens`` / cache-related counters).
    We read the fields we care about with ``.get(..., 0)`` defaults so
    a partial / unexpected SDK shape doesn't crash the role runner —
    losing one stats field is strictly better than losing the whole
    run. Same defensiveness for the wall-clock counters at the
    envelope level: they're typed ``int`` on the dataclass but absent
    in some degenerate ``is_error=True`` paths.

    foreman#244: also extract ``cache_creation_input_tokens`` and
    ``cache_read_input_tokens``. The Anthropic API charges these at
    25% and 10% of the regular input rate respectively. They're
    absent on the first turn of a fresh agent loop and on older API
    versions; the ``.get(..., 0)`` default keeps the no-cache path
    clean.

    foreman#266: hoisted out of ``anthropic_sdk.py`` so the recovery
    strategies (which must not import the concrete adapter) can reuse
    the same extraction logic when reconstructing usage from a
    captured ``ResultMessage`` mid-recovery.
    """
    usage_dict = getattr(message, "usage", None) or {}
    return UsageInfo(
        input_tokens=int(usage_dict.get("input_tokens", 0) or 0),
        output_tokens=int(usage_dict.get("output_tokens", 0) or 0),
        cache_creation_input_tokens=int(usage_dict.get("cache_creation_input_tokens", 0) or 0),
        cache_read_input_tokens=int(usage_dict.get("cache_read_input_tokens", 0) or 0),
        total_cost_usd=getattr(message, "total_cost_usd", None),
        model_usage=getattr(message, "model_usage", None),
        duration_ms=int(getattr(message, "duration_ms", 0) or 0),
        duration_api_ms=int(getattr(message, "duration_api_ms", 0) or 0),
        num_turns=int(getattr(message, "num_turns", 0) or 0),
    )
