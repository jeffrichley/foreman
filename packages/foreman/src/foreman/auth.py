"""GitHub App installation token minting.

Trades the App's private key for a short-lived (1-hour) installation token
scoped to one (App, repo) pair. The token authenticates as the App's bot
identity (e.g., ``foreman-planner[bot]``) for all PyGithub + git operations.

Flow per ``mint_installation_token``:

1. Read the App's RSA private key (PEM) from disk.
2. Sign a 10-minute JWT (RS256) with the App id in the ``iss`` claim.
3. Use that JWT to look up the App's installation id for the target repo
   (``GET /repos/{owner}/{repo}/installation``).
4. Exchange the JWT + installation id for a 1-hour installation token
   (``POST /app/installations/{installation_id}/access_tokens``).
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass
from pathlib import Path

import jwt
import requests


@dataclass(frozen=True)
class InstallationToken:
    """A minted installation token plus its expiry timestamp (unix seconds)."""

    token: str
    expires_at: int


def _read_private_key(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _mint_app_jwt(app_id: int, private_key: str) -> str:
    """Build a 10-minute JWT for the App. Used to call App-level endpoints.

    GitHub allows a maximum 10-minute lifetime; we use 9 minutes with a
    10-second backdated ``iat`` to stay inside GitHub's clock-skew tolerance.
    """
    now = int(time.time())
    payload = {
        "iat": now - 10,  # clock-skew tolerance
        "exp": now + 540,  # 9 min (GitHub max is 10)
        "iss": str(app_id),
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def _get_installation_id(app_jwt: str, repo_slug: str) -> int:
    """Look up the App's installation id for a given ``owner/repo``."""
    owner, repo = repo_slug.split("/", 1)
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/installation",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return int(resp.json()["id"])


def _parse_github_expires_at(value: str) -> int:
    """Convert GitHub's ISO 8601 ``expires_at`` (UTC, trailing Z) to unix seconds."""
    iso = value.replace("Z", "+00:00")
    return int(_dt.datetime.fromisoformat(iso).timestamp())


def mint_installation_token(
    app_id: int, private_key_path: Path | str, repo_slug: str
) -> InstallationToken:
    """Mint a 1-hour installation token for a ``(app, repo)`` pair.

    Parameters
    ----------
    app_id:
        The GitHub App's numeric id.
    private_key_path:
        Filesystem path to the App's RSA private key (PEM, PKCS#8 or PKCS#1).
    repo_slug:
        The target repository in ``owner/name`` form.
    """
    private_key = _read_private_key(private_key_path)
    app_jwt = _mint_app_jwt(app_id, private_key)
    installation_id = _get_installation_id(app_jwt, repo_slug)
    resp = requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return InstallationToken(
        token=str(data["token"]),
        expires_at=_parse_github_expires_at(str(data["expires_at"])),
    )
