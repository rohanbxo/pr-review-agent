from dataclasses import dataclass

from fastapi import Request

from app.config import get_settings
from app.netutil import parse_networks, resolve_client_ip


@dataclass(frozen=True)
class RequestContext:
    ip: str | None
    user_agent: str | None
    request_id: str | None


def get_request_context(request: Request) -> RequestContext:
    trusted = parse_networks(get_settings().trusted_proxies)
    peer = request.client.host if request.client else None
    return RequestContext(
        ip=resolve_client_ip(peer, request.headers.get("x-forwarded-for"), trusted),
        user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
    )
