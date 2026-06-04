"""Tests for the httpx-based GitHub GraphQL client."""

from __future__ import annotations

import httpx
import pytest

from foreman.reconciler.gh_graphql import HttpxGHGraphQLClient
from foreman.reconciler.observer import ObserverRateLimited, ObserverUnreachable


class _MockTransport(httpx.MockTransport):
    """Thin wrapper to capture the last request for assertions."""

    def __init__(self, handler) -> None:
        super().__init__(handler)
        self.last_request: httpx.Request | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.last_request = request
        return super().handle_request(request)


def _ok_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": {"repository": {"issues": {"nodes": []}, "pullRequests": {"nodes": []}}}},
    )


def _rate_limit_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        403,
        headers={"x-ratelimit-remaining": "0"},
        json={"message": "API rate limit exceeded"},
    )


def _500_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"message": "internal server error"})


def test_graphql_posts_to_v4_endpoint_with_bearer_token() -> None:
    transport = _MockTransport(_ok_handler)
    client = HttpxGHGraphQLClient(token="ghs_test", transport=transport)

    result = client.graphql("query { x }", {"a": 1})

    assert transport.last_request is not None
    assert str(transport.last_request.url) == "https://api.github.com/graphql"
    assert transport.last_request.method == "POST"
    assert transport.last_request.headers["authorization"] == "Bearer ghs_test"
    body = transport.last_request.read().decode("utf-8")
    assert '"query": "query { x }"' in body
    assert '"variables": {"a": 1}' in body
    assert result == {"data": {"repository": {"issues": {"nodes": []}, "pullRequests": {"nodes": []}}}}


def test_graphql_rate_limit_response_raises_typed_error() -> None:
    transport = _MockTransport(_rate_limit_handler)
    client = HttpxGHGraphQLClient(token="ghs_test", transport=transport)

    with pytest.raises(ObserverRateLimited):
        client.graphql("query { x }", {})


def test_graphql_500_response_raises_observer_unreachable() -> None:
    transport = _MockTransport(_500_handler)
    client = HttpxGHGraphQLClient(token="ghs_test", transport=transport)

    with pytest.raises(ObserverUnreachable):
        client.graphql("query { x }", {})


def test_graphql_network_error_raises_observer_unreachable() -> None:
    def _network_error(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("getaddrinfo failed")

    transport = _MockTransport(_network_error)
    client = HttpxGHGraphQLClient(token="ghs_test", transport=transport)

    with pytest.raises(ObserverUnreachable):
        client.graphql("query { x }", {})
