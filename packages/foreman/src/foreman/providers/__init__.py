"""Provider adapter implementations (Anthropic Agent SDK, etc.).

foreman#266: this package is the canonical public surface for the
provider boundary. Role runners import the domain exception family
(:class:`ProviderError` and friends) and the production factory
(:func:`make_provider`) from here — they do NOT import from
``claude_agent_sdk`` directly. A boundary-discipline test
(``packages/foreman/tests/test_provider_boundary.py``) enforces this
invariant by AST-scanning every module under
``packages/foreman/src/foreman/``.

Re-exports are deliberately scoped to symbols defined under
``foreman.providers.*`` submodules to avoid a circular import with
``foreman.provider`` (which re-roots its three legacy exceptions on
:class:`ProviderError`). Callers that need the legacy
``ProviderAuthError`` / ``StructuredOutputRetryError`` /
``StructuredOutputMissingError`` types keep importing them from
``foreman.provider`` as before; the boundary discipline is about
``claude_agent_sdk``, not about the legacy exception location.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from foreman.providers.exceptions import (
    ProviderError,
    ProviderTimeoutError,
    ProviderUnknownError,
)
from foreman.providers.recovery import (
    PartialResult,
    RecoveryChain,
    RecoveryStrategy,
)
from foreman.providers.strategies import SuccessAsErrorRecovery

if TYPE_CHECKING:
    # Type-only import keeps ``foreman.provider`` out of the runtime
    # import graph for this module — see the module docstring for the
    # circular-import discussion. At runtime ``make_provider`` returns
    # the concrete :class:`AnthropicSDKProvider`, which is-a
    # :class:`ProviderFacade`.
    from foreman.provider import ProviderFacade


def make_provider() -> ProviderFacade:
    """Construct the production provider with its deployed recovery chain.

    foreman#266 — the single audit boundary for the deployed recovery
    composition. Strategy registration order is significant: the first
    strategy whose ``can_recover`` returns ``True`` wins.

    Deployed strategies (in order):

    1. :class:`SuccessAsErrorRecovery` — foreman#230 — bare
       ``Exception("success")`` after a valid ``ResultMessage(subtype=
       "success")``. The most-observed SDK bug in production logs.

    Add new strategies HERE — one new file under
    ``foreman/providers/strategies/`` + one new line below. Every
    deployed bug workaround is visible at this site.
    """
    # Local import keeps the concrete adapter (which pulls in
    # ``claude_agent_sdk``) lazy and avoids a circular import with
    # ``foreman.provider`` — see the module docstring.
    from foreman.providers.anthropic_sdk import AnthropicSDKProvider

    recovery = RecoveryChain(
        [
            SuccessAsErrorRecovery(),
        ]
    )
    return AnthropicSDKProvider(recovery=recovery)


__all__ = [
    # Domain exception family (role runners catch these).
    "ProviderError",
    "ProviderInvalidResultError",
    "ProviderTimeoutError",
    "ProviderUnknownError",
    # Recovery pattern types (kept on the public surface so future
    # strategies can be unit-tested via the same imports as the
    # production strategies).
    "PartialResult",
    "RecoveryChain",
    "RecoveryStrategy",
    "SuccessAsErrorRecovery",
    # Production factory.
    "make_provider",
]
