"""X-Forwarded-For is client-controlled: only honoured from trusted proxies (CIDR-aware)."""

import httpx
import pytest
from sqlalchemy import select

import tests.helpers.shims  # noqa: F401  (must precede app imports)
from app.models import AuditLog, Role
from app.netutil import parse_networks, resolve_client_ip

TRUSTED = parse_networks(["172.16.0.0/12", "127.0.0.1/32"])


def test_parse_networks_supports_cidr_and_bare_ips():
    nets = parse_networks(["10.0.0.0/8", " 127.0.0.1 ", "", "::1", "192.168.1.7/24"])
    assert [str(n) for n in nets] == ["10.0.0.0/8", "127.0.0.1/32", "::1/128", "192.168.1.0/24"]


@pytest.mark.parametrize("peer,xff,expected", [
    # Untrusted peer: header ignored entirely, whatever it says.
    ("203.0.113.7", "1.2.3.4", "203.0.113.7"),
    ("203.0.113.7", "127.0.0.1", "203.0.113.7"),
    ("203.0.113.7", "172.16.0.1, 10.0.0.1", "203.0.113.7"),
    # Trusted peer (inside the CIDR, not a fixed IP): honoured.
    ("172.18.0.5", "198.51.100.20", "198.51.100.20"),
    ("127.0.0.1", "198.51.100.20", "198.51.100.20"),
    # Spoofed left-most entries are ignored: the right-most untrusted hop is the client.
    ("172.18.0.5", "6.6.6.6, 198.51.100.20", "198.51.100.20"),
    ("172.18.0.5", "6.6.6.6, 7.7.7.7, 198.51.100.20", "198.51.100.20"),
    # Trusted intermediate hops are skipped.
    ("172.18.0.5", "198.51.100.20, 172.20.0.3", "198.51.100.20"),
    ("172.18.0.5", "6.6.6.6, 198.51.100.20, 172.20.0.3, 172.31.255.254", "198.51.100.20"),
    # Just outside the /12.
    ("172.32.0.1", "198.51.100.20", "172.32.0.1"),
    # No header / empty header.
    ("172.18.0.5", None, "172.18.0.5"),
    ("172.18.0.5", "", "172.18.0.5"),
    ("172.18.0.5", " , ,", "172.18.0.5"),
    # Garbage never becomes the logged IP.
    ("172.18.0.5", "not-an-ip", "172.18.0.5"),
    ("172.18.0.5", "198.51.100.20, <script>", "172.18.0.5"),
    ("172.18.0.5", "198.51.100.20, 1.2.3.4:8080", "172.18.0.5"),
    ("172.18.0.5", "999.1.1.1", "172.18.0.5"),
    # IPv6 client behind a trusted proxy.
    ("172.18.0.5", "2001:db8::1", "2001:db8::1"),
    # Peer itself unparseable -> not trusted.
    ("testclient", "198.51.100.20", "testclient"),
    (None, "198.51.100.20", None),
])
def test_resolve_client_ip(peer, xff, expected):
    assert resolve_client_ip(peer, xff, TRUSTED) == expected


def test_resolved_value_is_always_an_ip_or_the_peer():
    import ipaddress

    for xff in ["x" * 5000, "1.1.1.1," * 1000, "\x00", "%0d%0a1.2.3.4", "1.2.3.4\r\nX-Evil: 1"]:
        got = resolve_client_ip("172.18.0.5", xff, TRUSTED)
        if got != "172.18.0.5":
            ipaddress.ip_address(got)


# --- end to end: the audit row's actor_ip ---------------------------------------------------

async def _denied_ip(session) -> str | None:
    session.expire_all()
    row = (await session.execute(select(AuditLog).order_by(AuditLog.id.desc()))).scalars().first()
    return row.actor_ip


async def _hit_forbidden(c: httpx.AsyncClient, headers: dict) -> None:
    r = await c.get("/audit", headers=headers)
    assert r.status_code == 403


async def test_xff_ignored_from_untrusted_peer_e2e(client, session, make_user, auth_headers):
    viewer = await make_user(Role.viewer)
    await _hit_forbidden(client, {**auth_headers(viewer), "X-Forwarded-For": "1.2.3.4"})
    assert await _denied_ip(session) == "203.0.113.7"


@pytest.fixture
async def proxied_client(app):
    transport = httpx.ASGITransport(app=app, client=("172.18.0.2", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.parametrize("xff,expected", [
    ("198.51.100.20", "198.51.100.20"),
    ("6.6.6.6, 198.51.100.20", "198.51.100.20"),
    ("garbage", "172.18.0.2"),
    (None, "172.18.0.2"),
])
async def test_xff_from_trusted_peer_e2e(proxied_client, session, make_user, auth_headers, xff, expected):
    viewer = await make_user(Role.viewer)
    headers = auth_headers(viewer)
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    await _hit_forbidden(proxied_client, headers)
    assert await _denied_ip(session) == expected


async def test_exchange_denial_uses_resolved_ip(proxied_client, session):
    r = await proxied_client.post(
        "/auth/github/exchange", json={"access_token": "t"},
        headers={"X-Auth-Bridge-Secret": "wrong", "X-Forwarded-For": "6.6.6.6, 198.51.100.99"},
    )
    assert r.status_code == 401
    assert await _denied_ip(session) == "198.51.100.99"
