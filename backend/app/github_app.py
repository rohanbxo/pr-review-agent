"""GitHub App installation-token minting.

This module performs the one POST the backend ever sends to GitHub
(`POST /app/installations/{id}/access_tokens`). It deliberately lives outside `app/agent/`:
the agent's read-only client must never be able to issue a non-GET request.

The token requested is down-scoped to read permissions even if the App was (mis)configured
with more, as defence in depth.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
import jwt

from app.config import get_settings

# Permissions requested for every installation token. The App itself should also only hold these.
READ_ONLY_PERMISSIONS = {"contents": "read", "pull_requests": "read", "metadata": "read"}

_REFRESH_MARGIN_S = 300


class GitHubAppError(Exception):
    pass


@dataclass
class _CachedToken:
    key: tuple[str, str]
    token: str
    expires_at: float  # epoch seconds


_cache: _CachedToken | None = None
_lock = asyncio.Lock()


def reset_cache() -> None:
    global _cache
    _cache = None


def _app_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    # iat backdated for clock skew; GitHub caps exp at 10 minutes.
    payload = {"iat": now - 60, "exp": now + 540, "iss": app_id}
    return jwt.encode(payload, private_key.replace("\\n", "\n"), algorithm="RS256")


def _parse_expiry(value: str | None) -> float:
    from datetime import datetime

    if not value:
        return time.time() + 3000
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time() + 3000


async def get_installation_token(*, transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """Installation token for the read-only GitHub App.

    Falls back to `settings.github_readonly_token` when no App is configured (dev).
    Returns None when neither is configured (public, unauthenticated access only).
    """
    global _cache
    s = get_settings()
    if not (s.github_app_id and s.github_app_private_key and s.github_app_installation_id):
        return s.github_readonly_token or None

    key = (s.github_app_id, s.github_app_installation_id)
    async with _lock:
        if _cache and _cache.key == key and _cache.expires_at - _REFRESH_MARGIN_S > time.time():
            return _cache.token

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {_app_jwt(s.github_app_id, s.github_app_private_key)}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-review-agent",
        }
        async with httpx.AsyncClient(base_url=s.github_api_url, transport=transport, timeout=15.0) as client:
            try:
                resp = await client.post(
                    f"/app/installations/{s.github_app_installation_id}/access_tokens",
                    headers=headers,
                    json={"permissions": READ_ONLY_PERMISSIONS},
                )
            except httpx.HTTPError as e:
                raise GitHubAppError(f"installation token request failed: {type(e).__name__}") from e
        if resp.status_code != 201:
            raise GitHubAppError(f"installation token request returned {resp.status_code}")
        body = resp.json()
        token = body.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubAppError("installation token missing from response")
        _cache = _CachedToken(key=key, token=token, expires_at=_parse_expiry(body.get("expires_at")))
        return token
