"""Unit tests for ``_translate_sdk_exception`` (foreman#266 sub-request 4).

The translator maps the SDK's exception types into the
``ProviderError`` family at the adapter boundary so role runners can
catch typed domain errors instead of importing ``claude_agent_sdk``.

Branches:

* Auth-prefix string → ``ProviderAuthError``
* ``asyncio.TimeoutError`` → ``ProviderTimeoutError``
* Anything else → ``ProviderUnknownError(str(exc))``

In every case the original is preserved as ``__cause__`` via
``raise translated from exc``.
"""

from __future__ import annotations

import pytest

from foreman.provider import ProviderAuthError
from foreman.providers.anthropic_sdk import _translate_sdk_exception
from foreman.providers.exceptions import (
    ProviderError,
    ProviderTimeoutError,
    ProviderUnknownError,
)


def test_translates_auth_prefix_to_provider_auth_error() -> None:
    """An ``Exception`` whose message starts with the SDK's
    ``"Claude Code returned an error result"`` prefix maps to
    ``ProviderAuthError`` (the foreman#227 auth-failure shape).
    """
    original = Exception("Claude Code returned an error result: foo")
    translated = _translate_sdk_exception(original)

    assert isinstance(translated, ProviderAuthError)
    assert isinstance(translated, ProviderError)


def test_translates_asyncio_timeout_to_provider_timeout_error() -> None:
    """``asyncio.TimeoutError`` from the SDK maps to ``ProviderTimeoutError``."""
    original = TimeoutError()
    translated = _translate_sdk_exception(original)

    assert isinstance(translated, ProviderTimeoutError)
    assert isinstance(translated, ProviderError)


def test_translates_unknown_to_provider_unknown_error() -> None:
    """An exception we don't recognize maps to ``ProviderUnknownError``."""
    original = Exception("weird unknown failure")
    translated = _translate_sdk_exception(original)

    assert isinstance(translated, ProviderUnknownError)
    assert isinstance(translated, ProviderError)


def test_translated_exception_preserves_original_via_chained_raise() -> None:
    """When the adapter actually raises the translated exception via
    ``raise translated from exc``, ``__cause__`` is the original SDK
    exception.
    """
    original = Exception("weird unknown failure")
    translated = _translate_sdk_exception(original)

    with pytest.raises(ProviderUnknownError) as exc_info:
        raise translated from original

    assert exc_info.value.__cause__ is original


def test_unknown_translation_carries_original_message() -> None:
    """``ProviderUnknownError(str(exc))`` keeps the original message
    visible without re-imports of ``claude_agent_sdk``.
    """
    original = Exception("totally weird")
    translated = _translate_sdk_exception(original)

    assert "totally weird" in str(translated)
