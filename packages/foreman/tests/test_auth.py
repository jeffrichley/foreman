"""Tests for GitHub App installation-token minting (foreman.auth)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from foreman.auth import (
    InstallationToken,
    _mint_app_jwt,
    mint_installation_token,
)


def _generate_private_key_pem() -> str:
    """Generate a fresh RSA-2048 key in PEM (PKCS#8) form for tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("utf-8")


@pytest.fixture
def private_key_pem() -> str:
    return _generate_private_key_pem()


@pytest.fixture
def private_key_file(tmp_path: Path, private_key_pem: str) -> Path:
    p = tmp_path / "planner.pem"
    p.write_text(private_key_pem, encoding="utf-8")
    return p


def test_mint_app_jwt_has_correct_claims(private_key_pem: str) -> None:
    """The minted App JWT must carry the App id as the `iss` claim, plus
    iat/exp in a 10-min window."""
    app_id = 123456
    token = _mint_app_jwt(app_id, private_key_pem)
    # Decode using the matching public key
    public_key_pem = (
        serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    claims = jwt.decode(token, public_key_pem, algorithms=["RS256"])
    assert claims["iss"] == str(app_id)
    now = int(time.time())
    assert claims["iat"] <= now
    assert 0 < claims["exp"] - now <= 600


def test_mint_installation_token_calls_github_and_returns_token(
    private_key_file: Path,
) -> None:
    """Full happy-path: get installation id, exchange for installation token."""
    fake_get = MagicMock()
    fake_get.return_value.json.return_value = {"id": 9999}
    fake_get.return_value.raise_for_status.return_value = None

    fake_post = MagicMock()
    # GitHub returns ISO 8601 in UTC with trailing Z
    fake_post.return_value.json.return_value = {
        "token": "ghs_installationtoken123",
        "expires_at": "2099-01-01T00:00:00Z",
    }
    fake_post.return_value.raise_for_status.return_value = None

    with (
        patch("foreman.auth.requests.get", fake_get),
        patch("foreman.auth.requests.post", fake_post),
    ):
        result = mint_installation_token(
            app_id=42, private_key_path=private_key_file, repo_slug="jeffrichley/voice"
        )

    assert isinstance(result, InstallationToken)
    assert result.token == "ghs_installationtoken123"
    # Year 2099 → expires_at unix seconds well beyond now
    assert result.expires_at > int(time.time()) + 60 * 60 * 24 * 365

    # Verify the right endpoints were hit
    get_url = fake_get.call_args.args[0]
    assert get_url == "https://api.github.com/repos/jeffrichley/voice/installation"
    post_url = fake_post.call_args.args[0]
    assert post_url == "https://api.github.com/app/installations/9999/access_tokens"

    # Both calls must carry a Bearer JWT
    get_headers = fake_get.call_args.kwargs["headers"]
    assert get_headers["Authorization"].startswith("Bearer ey")  # JWT starts with "eyJ"
    post_headers = fake_post.call_args.kwargs["headers"]
    assert post_headers["Authorization"].startswith("Bearer ey")


def test_mint_installation_token_raises_on_http_error(private_key_file: Path) -> None:
    """If GitHub returns a 4xx/5xx, the raise_for_status() must surface it."""
    import requests as _requests

    fake_get = MagicMock()
    fake_get.return_value.raise_for_status.side_effect = _requests.HTTPError("404 not found")

    with (
        patch("foreman.auth.requests.get", fake_get),
        pytest.raises(_requests.HTTPError),
    ):
        mint_installation_token(
            app_id=42, private_key_path=private_key_file, repo_slug="jeffrichley/voice"
        )
