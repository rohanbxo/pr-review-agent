"""User trust domain: who is this GitHub user, and may they read this repo?

Separate from the agent's read-only client on purpose (different credentials, different
questions). This module must not import from `app.agent`.

- `fetch_identity` uses the USER's OAuth token (scopes `read:user user:email read:org`).
- `can_read_repo` uses the App installation token (or the dev read-only token) to ask GitHub
  whether a specific user can read a specific repo. It fails closed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

from app.config import get_settings
from app.github_app import get_installation_token
from app.validation import validate_repo_full_name

log = logging.getLogger(__name__)

_API_VERSION = "2022-11-28"
_MAX_PAGES = 10
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


class GitHubIdentityError(Exception):
    """GitHub did not confirm the identity (bad/expired token, API error, malformed response)."""


@dataclass
class GitHubIdentity:
    id: int
    login: str
    email: str | None
    avatar_url: str | None
    orgs: list[str] = field(default_factory=list)


def get_github_transport() -> httpx.AsyncBaseTransport | None:
    """FastAPI dependency: the transport used for GitHub identity calls.
    Production returns None (real network); tests override it with an httpx.MockTransport."""
    return None


def _headers(token: str | None) -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _API_VERSION,
        "User-Agent": "pr-review-agent",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _client(token: str | None, transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=get_settings().github_api_url,
        headers=_headers(token),
        transport=transport,
        timeout=15.0,
        follow_redirects=False,
    )


async def _get_paginated(client: httpx.AsyncClient, path: str) -> list:
    items: list = []
    url: str | None = path
    params: dict | None = {"per_page": 100}
    for _ in range(_MAX_PAGES):
        if url is None:
            break
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            raise GitHubIdentityError(f"GET {path} returned {resp.status_code}")
        page = resp.json()
        if not isinstance(page, list):
            raise GitHubIdentityError(f"GET {path} returned a non-list body")
        items.extend(page)
        nxt = resp.links.get("next", {}).get("url")
        # Only follow pagination links that stay on the configured API host.
        if nxt and httpx.URL(nxt).host == httpx.URL(get_settings().github_api_url).host:
            url, params = nxt, None
        else:
            url = None
    return items


async def fetch_identity(
    access_token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> GitHubIdentity:
    """Re-read the identity from GitHub. Nothing the caller claims about the user is trusted."""
    if not access_token:
        raise GitHubIdentityError("empty access token")
    try:
        async with _client(access_token, transport) as client:
            resp = await client.get("/user")
            if resp.status_code != 200:
                raise GitHubIdentityError(f"GET /user returned {resp.status_code}")
            user = resp.json()
            if not isinstance(user, dict):
                raise GitHubIdentityError("GET /user returned a non-object body")
            gid, login = user.get("id"), user.get("login")
            if not isinstance(gid, int) or isinstance(gid, bool) or gid <= 0:
                raise GitHubIdentityError("GET /user returned no valid id")
            if not isinstance(login, str) or not _LOGIN_RE.fullmatch(login):
                raise GitHubIdentityError("GET /user returned no valid login")

            email = _primary_verified_email(await _get_paginated(client, "/user/emails"))
            orgs_raw = await _get_paginated(client, "/user/orgs")
    except httpx.HTTPError as e:
        raise GitHubIdentityError(f"GitHub request failed: {type(e).__name__}") from e
    except ValueError as e:  # JSON decode
        raise GitHubIdentityError("GitHub returned malformed JSON") from e

    orgs = [o["login"] for o in orgs_raw if isinstance(o, dict) and isinstance(o.get("login"), str)]
    avatar = user.get("avatar_url")
    return GitHubIdentity(
        id=gid,
        login=login,
        email=email,
        avatar_url=avatar if isinstance(avatar, str) else None,
        orgs=orgs,
    )


def _primary_verified_email(emails: list) -> str | None:
    """Only a VERIFIED address is used; the public profile email is unverified and ignored."""
    verified = [
        e for e in emails
        if isinstance(e, dict) and e.get("verified") is True and isinstance(e.get("email"), str)
    ]
    for e in verified:
        if e.get("primary") is True:
            return e["email"]
    return verified[0]["email"] if verified else None


async def can_read_repo(
    *, user_login: str, repo_full_name: str, transport: httpx.AsyncBaseTransport | None = None
) -> bool:
    """Ask GitHub whether THIS user can read THIS repo. Any error → False (fail closed).

    Org membership is coarser than repo permissions, so an `owner/*` grant alone is not enough.
    """
    try:
        validate_repo_full_name(repo_full_name)
    except ValueError:
        return False
    if not isinstance(user_login, str) or not _LOGIN_RE.fullmatch(user_login):
        return False
    owner, repo = repo_full_name.split("/")

    try:
        token = await get_installation_token(transport=transport)
        async with _client(token, transport) as client:
            resp = await client.get(f"/repos/{owner}/{repo}")
            if resp.status_code != 200:
                return False
            meta = resp.json()
            if not isinstance(meta, dict):
                return False
            if meta.get("private") is False and meta.get("visibility", "public") == "public":
                return True
            if not token:
                return False
            resp = await client.get(f"/repos/{owner}/{repo}/collaborators/{user_login}/permission")
            if resp.status_code != 200:
                return False
            body = resp.json()
            perm = body.get("permission") if isinstance(body, dict) else None
            return isinstance(perm, str) and perm in {"read", "triage", "write", "maintain", "admin"}
    except Exception as e:  # noqa: BLE001 — fail closed on anything
        log.warning("can_read_repo failed closed for %s on %s: %s", user_login, repo_full_name, type(e).__name__)
        return False
