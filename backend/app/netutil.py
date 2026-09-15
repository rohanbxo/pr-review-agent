"""Client IP resolution.

X-Forwarded-For is client-controlled. It is only read when the TCP peer is a trusted proxy,
and trusted proxies may be CIDR blocks (under compose the proxy's address comes from a subnet).
"""

import ipaddress
from collections.abc import Iterable

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(entries: Iterable[str]) -> list[Network]:
    return [ipaddress.ip_network(e.strip(), strict=False) for e in entries if e.strip()]


def _is_trusted(ip: str, networks: list[Network]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in networks)


def resolve_client_ip(peer_ip: str | None, xff: str | None, trusted: list[Network]) -> str | None:
    """Walk X-Forwarded-For right to left, skipping trusted hops; the first untrusted hop is the client."""
    if peer_ip is None:
        return None
    if not xff or not _is_trusted(peer_ip, trusted):
        return peer_ip
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            # Garbage in the header: stop trusting it and fall back to the last good answer.
            return peer_ip
        if not _is_trusted(hop, trusted):
            return hop
    return hops[0] if hops else peer_ip
