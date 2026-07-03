"""Domain exception hierarchy for the provider boundary (foreman#266).

Role runners catch ``ProviderError`` and its subclasses; they do NOT
import from ``claude_agent_sdk``. The adapter
(``foreman.providers.anthropic_sdk``) is responsible for translating
SDK-specific exception types into the corresponding domain type at
the boundary, preserving the original via ``__cause__``.

The three pre-existing exceptions in ``foreman.provider``
(``ProviderAuthError``, ``StructuredOutputRetryError``,
``StructuredOutputMissingError``) are re-rooted to inherit from
``ProviderError`` so a single ``except ProviderError`` arm in each
role runner catches every provider-boundary failure. Multiple
inheritance with ``RuntimeError`` preserves their pre-#266 lineage
so any existing ``isinstance(exc, RuntimeError)`` check still works.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for every provider-boundary error.

    Role runners catch this. The adapter raises subclasses with the
    original SDK exception preserved as ``__cause__``.
    """


class ProviderTimeoutError(ProviderError):
    """The provider's underlying transport timed out.

    Translated from ``asyncio.TimeoutError`` (and any equivalent SDK
    timeout shape) at the adapter boundary.
    """


class ProviderUnknownError(ProviderError):
    """The provider raised an exception the adapter could not classify.

    Carries the original exception's message in ``args[0]`` (so
    ``str(exc)`` surfaces it) and the original instance in
    ``__cause__`` (via the adapter's ``raise ... from exc``). Role
    runners that need the original type can introspect ``__cause__``.
    """


class ProviderTransientError(ProviderError):
    """Anthropic-side transient transport failure.

    Covers 5xx, 429, connection refused, and transport-level timeout.
    Re-raised at the provider boundary; roles must surface it as
    ``Outcome(kind=TRANSIENT_PROVIDER_ERROR)``, not ``ERROR``.

    foreman#361: distinguished from :class:`ProviderUnknownError` so
    the v4 state machine can exempt these from the
    ``max_state_attempts`` runaway-defense cap and schedule a delayed
    retry on an exponential-backoff schedule.
    """
