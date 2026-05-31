"""Smoke tests for AnthropicSDKProvider — verifies wiring without real API calls.

Real end-to-end agent runs are gated behind the `real_engine` pytest marker
(per the never-done-without-running rule). This file covers structural
correctness of the adapter; the live integration test lives separately.
"""

from __future__ import annotations

from foreman.provider import ProviderFacade
from foreman.providers.anthropic_sdk import AnthropicSDKProvider


def test_provider_can_be_instantiated() -> None:
    provider = AnthropicSDKProvider()
    assert isinstance(provider, ProviderFacade)


def test_provider_inherits_from_facade() -> None:
    assert issubclass(AnthropicSDKProvider, ProviderFacade)
