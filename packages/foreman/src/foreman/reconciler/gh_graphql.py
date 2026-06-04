"""httpx-based implementation of the v3 GHGraphQLClient Protocol.

Uses GitHub's GraphQL v4 endpoint (https://api.github.com/graphql) with
Bearer-token auth. Tokens come from foreman.identity.IdentityRegistry (the
existing v2 App-installation token machinery). Failures map to the typed
exceptions the observer expects.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from foreman.reconciler.observer import ObserverError, ObserverRateLimited, ObserverUnreachable

_GRAPHQL_ENDPOINT = "https://api.github.com/graphql"
_DEFAULT_TIMEOUT = 30.0


class HttpxGHGraphQLClient:
    """Bearer-token httpx wrapper around GitHub's GraphQL v4 endpoint.

    `token` is an App-installation token (ghs_xxx) — same shape v2 uses for
    REST. GitHub treats it identically for GraphQL when the App has matching
    GraphQL permissions (foreman planner App already does).
    """

    def __init__(
        self,
        *,
        token: str,
        timeout: float = _DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST a GraphQL query. Maps failures to observer typed exceptions."""
        # Use stdlib json.dumps (default separators include spaces after `:`
        # and `,`) instead of httpx's compact encoder so the wire body is
        # human-readable when captured in logs / fixtures.
        body = json.dumps({"query": query, "variables": variables})
        try:
            response = self._client.post(
                _GRAPHQL_ENDPOINT,
                content=body,
                headers={"Content-Type": "application/json"},
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise ObserverUnreachable(str(exc)) from exc
        except httpx.RequestError as exc:
            raise ObserverUnreachable(str(exc)) from exc

        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            raise ObserverRateLimited(
                f"GitHub rate limit exceeded: {response.text[:200]}"
            )

        if response.status_code >= 500:
            raise ObserverUnreachable(
                f"GitHub returned {response.status_code}: {response.text[:200]}"
            )

        if response.status_code >= 400:
            raise ObserverError(
                f"GitHub returned {response.status_code}: {response.text[:200]}"
            )

        return response.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpxGHGraphQLClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
