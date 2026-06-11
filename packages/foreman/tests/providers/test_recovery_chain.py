"""Unit tests for ``RecoveryChain`` (foreman#266 sub-request 3).

Chain semantics: try each registered strategy in order; the first one
whose ``can_recover`` returns True handles the exception. If none
match, ``try_recover`` returns ``None`` (so the caller can fall through
to translation / re-raise).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from foreman.provider import UsageInfo
from foreman.providers.recovery import (
    PartialResult,
    RecoveryChain,
    RecoveryStrategy,
)


class _FakeStrategy(RecoveryStrategy):
    """Test double that returns a configured ``can_recover`` verdict and
    records whether ``recover`` was called.
    """

    def __init__(self, *, verdict: bool, payload: tuple[Any, UsageInfo]) -> None:
        self._verdict = verdict
        self._payload = payload
        self.can_recover_called = False
        self.recover_called = False

    def can_recover(self, exc: BaseException, partial: PartialResult[Any]) -> bool:
        self.can_recover_called = True
        return self._verdict

    def recover(self, exc: BaseException, partial: PartialResult[Any]) -> tuple[Any, UsageInfo]:
        self.recover_called = True
        return self._payload


def _empty_partial() -> PartialResult[Any]:
    return PartialResult(result_message=None, output_model=None)


def test_first_matching_strategy_handles() -> None:
    """When the first registered strategy's ``can_recover`` returns
    True, the chain invokes its ``recover`` and short-circuits.
    """
    payload_a = ("alpha", UsageInfo())
    payload_b = ("bravo", UsageInfo())
    s_a = _FakeStrategy(verdict=True, payload=payload_a)
    s_b = _FakeStrategy(verdict=True, payload=payload_b)

    chain = RecoveryChain([s_a, s_b])
    result = chain.try_recover(Exception("boom"), _empty_partial())

    assert result == payload_a
    assert s_a.recover_called is True
    assert s_b.recover_called is False


def test_order_matters_when_only_second_matches() -> None:
    """When the first strategy declines and the second matches, the
    second one's ``recover`` is invoked.
    """
    payload_a = ("alpha", UsageInfo())
    payload_b = ("bravo", UsageInfo())
    s_a = _FakeStrategy(verdict=False, payload=payload_a)
    s_b = _FakeStrategy(verdict=True, payload=payload_b)

    chain = RecoveryChain([s_a, s_b])
    result = chain.try_recover(Exception("boom"), _empty_partial())

    assert result == payload_b
    assert s_a.can_recover_called is True
    assert s_a.recover_called is False
    assert s_b.recover_called is True


def test_returns_none_when_none_match() -> None:
    """When no strategy matches, ``try_recover`` returns ``None`` so the
    caller can fall through to translation / re-raise.
    """
    s_a = _FakeStrategy(verdict=False, payload=("alpha", UsageInfo()))
    s_b = _FakeStrategy(verdict=False, payload=("bravo", UsageInfo()))

    chain = RecoveryChain([s_a, s_b])
    result = chain.try_recover(Exception("boom"), _empty_partial())

    assert result is None
    assert s_a.recover_called is False
    assert s_b.recover_called is False


def test_logs_strategy_class_name_on_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When recovery fires, the chain emits one INFO log line naming the
    strategy class + exception type so log analysis can graph per-bug
    fire counts without code changes.
    """
    payload = ("alpha", UsageInfo())
    s = _FakeStrategy(verdict=True, payload=payload)

    chain = RecoveryChain([s])
    caplog.set_level(logging.INFO, logger="foreman.providers.recovery")
    chain.try_recover(Exception("boom"), _empty_partial())

    matching = [r for r in caplog.records if "provider recovery" in r.getMessage()]
    assert len(matching) == 1
    msg = matching[0].getMessage()
    assert "_FakeStrategy" in msg
    assert "Exception" in msg


def test_empty_chain_returns_none() -> None:
    """An empty chain matches nothing; ``try_recover`` returns ``None``."""
    chain = RecoveryChain([])
    assert chain.try_recover(Exception("boom"), _empty_partial()) is None
