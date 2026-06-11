"""Recovery-strategy pattern for known SDK bug shapes (foreman#266).

This module defines the abstract :class:`RecoveryStrategy` ABC,
the :class:`PartialResult` dataclass mid-iteration state strategies
can inspect, and the :class:`RecoveryChain` that walks registered
strategies in order.

A strategy is one known SDK bug shape we can recover from. Each
strategy's ``can_recover`` predicate is intentionally narrow — it
inspects the exception's type, args tuple, and the captured
mid-stream state — so genuine SDK failures fall through to the
translator instead of being mis-classified as fake successes.

Concrete strategies live under ``foreman.providers.strategies``.
Registration is explicit (no auto-discovery): the deployed chain is
constructed in ``foreman.providers.make_provider`` and the order is
documented at the construction site.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from foreman.provider import UsageInfo

_log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass
class PartialResult[T: BaseModel]:
    """State captured mid-iteration that recovery strategies can read.

    The adapter populates this object as the SDK message stream is
    consumed; if the SDK then raises a known bug shape, the registered
    strategies inspect ``result_message`` (the most recent
    ``ResultMessage`` observed, if any) + ``output_model`` (the
    Pydantic schema the caller asked for) to decide whether they can
    return a valid result anyway.

    Attributes:
        result_message: The most recent ``ResultMessage`` the adapter
            saw mid-stream, or ``None`` if the SDK raised before
            yielding any. Typed ``Any`` because :mod:`foreman.providers`
            is the only place ``claude_agent_sdk`` types are allowed;
            strategies access fields via ``getattr`` to avoid pulling
            the SDK into the strategy import graph.
        output_model: The Pydantic model class the caller asked the
            adapter to validate against. The recovery strategy uses
            this to re-validate the captured ``structured_output``.
    """

    result_message: Any | None = None
    output_model: type[T] | None = None


class RecoveryStrategy(ABC):
    """One known SDK bug shape we know how to recover from.

    Each concrete subclass owns a narrow predicate (``can_recover``)
    that inspects the exception + the captured mid-stream state, and
    a recovery method (``recover``) that returns the
    ``(validated_output, usage)`` tuple the adapter would have
    returned on the happy path.

    Subclasses MUST keep ``can_recover`` strict — type checks, args
    equality, presence of expected partial state — so genuine SDK
    failures fall through to the translator instead of being silently
    re-classified.
    """

    @abstractmethod
    def can_recover(self, exc: BaseException, partial: PartialResult[Any]) -> bool:
        """Return True iff this strategy can produce a valid result
        for the captured exception + mid-stream state.
        """

    @abstractmethod
    def recover(self, exc: BaseException, partial: PartialResult[T]) -> tuple[T, UsageInfo]:
        """Return the ``(validated_output, usage)`` tuple the caller
        would have received on the happy path.

        Only invoked by :class:`RecoveryChain` after ``can_recover``
        returned True for this strategy on the same inputs.
        """


class RecoveryChain:
    """Try each registered strategy in order; first match handles.

    Order matters: when two strategies both claim the same exception
    shape, the earlier one in the registration list wins. The
    deployed order lives at the chain's construction site (see
    :func:`foreman.providers.make_provider`) and is the single place
    the chain composition is audited.
    """

    def __init__(self, strategies: list[RecoveryStrategy]) -> None:
        self._strategies = tuple(strategies)

    def try_recover(
        self, exc: BaseException, partial: PartialResult[T]
    ) -> tuple[T, UsageInfo] | None:
        """Return the first strategy's recovery output, or ``None`` if
        no strategy claims the exception.

        When a strategy fires, emit one INFO-level log line naming
        the strategy class + exception type. Log analysis can graph
        per-strategy fire counts from this line without further code
        changes.
        """
        for strategy in self._strategies:
            if strategy.can_recover(exc, partial):
                _log.info(
                    "provider recovery: %s handled %s",
                    strategy.__class__.__name__,
                    type(exc).__name__,
                )
                return strategy.recover(exc, partial)
        return None
