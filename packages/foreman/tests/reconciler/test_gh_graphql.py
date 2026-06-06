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


# --------------------------------------------------------------------
# foreman#142 — token_supplier refreshes the Authorization header per
# request so an installation-token expiry doesn't permanently 401 the
# daemon (which is what happens when a static `token=` is captured in
# httpx.Client headers at construction time).
# --------------------------------------------------------------------


def test_graphql_calls_token_supplier_on_each_request() -> None:
    """Each ``graphql()`` call must invoke the supplier — proves the daemon
    re-reads the current installation token instead of caching the value
    captured at client construction."""
    transport = _MockTransport(_ok_handler)
    tokens = iter(["ghs_first", "ghs_second", "ghs_third"])
    call_count = {"n": 0}

    def supplier() -> str:
        call_count["n"] += 1
        return next(tokens)

    client = HttpxGHGraphQLClient(token_supplier=supplier, transport=transport)

    client.graphql("query { x }", {})
    assert call_count["n"] == 1
    assert transport.last_request is not None
    assert transport.last_request.headers["authorization"] == "Bearer ghs_first"

    client.graphql("query { x }", {})
    assert call_count["n"] == 2
    assert transport.last_request.headers["authorization"] == "Bearer ghs_second"

    client.graphql("query { x }", {})
    assert call_count["n"] == 3
    assert transport.last_request.headers["authorization"] == "Bearer ghs_third"


def test_graphql_token_supplier_post_rotation_is_picked_up() -> None:
    """Direct repro of the foreman#142 daemon failure mode: a token that
    expires mid-life is replaced by the supplier, and the next request
    uses the new value rather than the cached header. Pre-fix this would
    keep using ``ghs_stale`` and 401 forever."""
    transport = _MockTransport(_ok_handler)
    current = {"value": "ghs_stale"}
    client = HttpxGHGraphQLClient(
        token_supplier=lambda: current["value"], transport=transport
    )

    client.graphql("query { x }", {})
    assert transport.last_request is not None
    assert transport.last_request.headers["authorization"] == "Bearer ghs_stale"

    current["value"] = "ghs_fresh"

    client.graphql("query { x }", {})
    assert transport.last_request.headers["authorization"] == "Bearer ghs_fresh"


def test_graphql_requires_exactly_one_of_token_or_supplier() -> None:
    """Construction with both ``token`` and ``token_supplier`` — or neither —
    is a programming error and should fail loudly at construction, not at
    first request."""
    with pytest.raises(ValueError, match="exactly one"):
        HttpxGHGraphQLClient()

    with pytest.raises(ValueError, match="exactly one"):
        HttpxGHGraphQLClient(token="ghs_x", token_supplier=lambda: "ghs_y")
