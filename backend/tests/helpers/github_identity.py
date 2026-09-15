"""A fake GitHub for identity tests, served through httpx.MockTransport (no network)."""

from dataclasses import dataclass, field

import httpx


@dataclass
class FakeAccount:
    id: int
    login: str
    orgs: list[str] = field(default_factory=list)
    email: str | None = None
    avatar_url: str | None = None


class FakeGitHub:
    def __init__(self) -> None:
        self.accounts: dict[str, FakeAccount] = {}
        self.requests: list[httpx.Request] = []

    def add(self, token: str, *, id: int, login: str, orgs=(), email=None) -> FakeAccount:
        acct = FakeAccount(
            id=id, login=login, orgs=list(orgs), email=email or f"{login}@example.com",
            avatar_url=f"https://avatars.example/{id}",
        )
        self.accounts[token] = acct
        return acct

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method != "GET":
            return httpx.Response(405)
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        acct = self.accounts.get(token)
        if acct is None:
            return httpx.Response(401, json={"message": "Bad credentials"})
        path = request.url.path
        if path == "/user":
            # Deliberately includes an unverified public email that must not be used.
            return httpx.Response(200, json={
                "id": acct.id, "login": acct.login, "avatar_url": acct.avatar_url,
                "email": "unverified-public@evil.example",
            })
        if path == "/user/emails":
            return httpx.Response(200, json=[
                {"email": "spoof@evil.example", "primary": False, "verified": False},
                {"email": acct.email, "primary": True, "verified": True},
            ])
        if path == "/user/orgs":
            return httpx.Response(200, json=[{"login": o, "id": i} for i, o in enumerate(acct.orgs)])
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
