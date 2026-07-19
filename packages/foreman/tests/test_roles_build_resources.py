"""Direct tests for ``foreman.roles.build_role_resources`` (roles-layer
extension, Task 7). ``build_role_resources`` used to fetch the bot's App
metadata (slug + numeric id) via a direct ``GET /app`` PEM-signed call
(:func:`foreman.auth.fetch_app_metadata`); it now sources the slug from
``registry.get_app_slug(role)`` so the helper works PEM-free with
:class:`~foreman.v4.identity.SandboxIdentityRegistry` (Task 6) as well as
:class:`~foreman.v4.identity.V4IdentityRegistry`.
"""

from __future__ import annotations

import pytest

import foreman.roles as roles_module
from foreman import auth as foreman_auth
from foreman.git_host import BotIdentity
from foreman.roles import build_role_resources


class _FakeRegistry:
    """Duck-typed registry satisfying ``build_role_resources``'s production
    contract: ``get_role_token(role) -> str`` and (Task 6) ``get_app_slug(role)
    -> str``."""

    def __init__(self, *, token: str, slug: str) -> None:
        self._token = token
        self._slug = slug
        self.slug_calls: list[str] = []

    def get_role_token(self, role: str) -> str:
        return self._token

    def get_app_slug(self, role: str) -> str:
        self.slug_calls.append(role)
        return self._slug


def test_build_role_resources_sources_slug_from_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The returned host's ``BotIdentity.slug`` comes from
    ``registry.get_app_slug`` — never from a direct ``GET /app`` fetch."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("fetch_app_metadata must not be called")

    # Patched at its defining module so the assertion holds regardless of
    # whether foreman.roles still imports the symbol.
    monkeypatch.setattr(foreman_auth, "fetch_app_metadata", _boom)

    captured: dict[str, object] = {}

    class _RecordingProvider:
        def __init__(self, *, identity: BotIdentity, client: object) -> None:
            captured["identity"] = identity
            captured["client"] = client

    monkeypatch.setattr(roles_module, "GitHubProvider", _RecordingProvider)

    registry = _FakeRegistry(token="ghs_test_token", slug="foreman-planner")

    host, token, client = build_role_resources(
        registry=registry,
        role="planner",
        app_id=4242,
    )

    assert token == "ghs_test_token"
    assert registry.slug_calls == ["planner"]
    assert captured["identity"] == BotIdentity(
        slug="foreman-planner", user_id=4242, token="ghs_test_token"
    )
    assert isinstance(host, _RecordingProvider)
    assert client is not None
