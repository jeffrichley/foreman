"""httpx-based implementation of the v3 GHGraphQLClient Protocol.

Uses GitHub's GraphQL v4 endpoint (https://api.github.com/graphql) with
Bearer-token auth. Tokens come from foreman.identity.IdentityRegistry (the
existing v2 App-installation token machinery). Failures map to the typed
exceptions the observer expects.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from foreman.reconciler.observer import ObserverError, ObserverRateLimited, ObserverUnreachable

_GRAPHQL_ENDPOINT = "https://api.github.com/graphql"
_DEFAULT_TIMEOUT = 30.0


class HttpxGHGraphQLClient:
    """Bearer-token httpx wrapper around GitHub's GraphQL v4 endpoint.

    Two construction modes:

    ``token_supplier`` — a zero-arg callable returning a current installation
        token string. Called on every ``graphql()`` request. This is the right
        shape when the underlying token is short-lived (App-installation tokens
        expire after 1 hour) and the caller has a refresh mechanism upstream
        (e.g., ``IdentityRegistry._get_cached`` already re-mints when a cached
        token is near expiry).

    ``token`` — a static token string (backwards compatibility). The token is
        captured once and reused for the client's entire lifetime. Use this
        only for short-lived clients (tests, one-shot scripts) where you know
        the token will not expire mid-lifetime.

    Exactly one of ``token`` / ``token_supplier`` must be provided.

    Why this shape: when the token is embedded once in ``httpx.Client.headers``
    (as the legacy ``token`` path does), httpx caches that header forever and
    the daemon silently 401s when the installation token expires. Daemon
    observers — which run as long as the process — must use ``token_supplier``
    so each poll picks up a fresh token. See foreman#142 for the post-mortem.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        token_supplier: Callable[[], str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if (token is None) == (token_supplier is None):
            raise ValueError(
                "exactly one of `token` or `token_supplier` must be provided"
            )
        if token_supplier is not None:
            self._token_supplier: Callable[[], str] = token_supplier
        else:
            # Static-token path: wrap in a constant supplier so the request
            # path is uniform. We still mint the request-time header per
            # call; the supplier is just a thunk.
            static = token
            assert static is not None  # narrow for mypy
            self._token_supplier = lambda: static
        # Disable keepalive entirely (max_keepalive_connections=0).
        # See foreman#166: with the default pool, a CLOSE_WAIT socket can
        # accumulate after the remote sends FIN, and httpx happily keeps it
        # in the pool. The next request reuses the dead connection and
        # silently blocks. The daemon makes ~1 GraphQL call per project
        # per poll cycle (~60s), so a fresh TLS handshake per call costs
        # ~50-100ms — irrelevant at that cadence, eliminates the wedge.
        #
        # `pool=5.0` is the connection-pool acquire timeout (fast-fail if
        # we somehow can't get a slot); the per-phase read/connect/write
        # timeouts replace the prior single-float ``timeout`` (still kept
        # as the ``Timeout(...)`` first-positional default so callers
        # that pass a custom float still work).
        self._client = httpx.Client(
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=0),
            timeout=httpx.Timeout(
                timeout,
                connect=10.0,
                read=30.0,
                write=10.0,
                pool=5.0,
            ),
            transport=transport,
            headers={
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
        # Mint the Authorization header per request from the token supplier.
        # IdentityRegistry's supplier transparently re-mints when the cached
        # installation token is near expiry — no extra plumbing needed here.
        token = self._token_supplier()
        try:
            response = self._client.post(
                _GRAPHQL_ENDPOINT,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
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

    def __enter__(self) -> HttpxGHGraphQLClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
