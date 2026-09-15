"""Read-only GitHub REST client for the agent.

Read-only is enforced HERE, at the transport, not in the prompt (SPEC § Non-negotiables 1):

* only GET and HEAD are allowed;
* the path must full-match one of a small set of anchored regexes;
* both checks run before any request object reaches the transport, so a violation never
  opens a socket — but the attempt is still recorded (``blocked=True``) so it shows up in the
  persisted call log;
* redirects are never followed automatically; a same-host redirect whose target is itself on
  the allowlist is followed manually (GitHub 301s renamed repos), anything else is refused.

User identity calls (``/user`` …) live in ``app/github_identity.py`` — a different trust domain.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import get_settings

__all__ = ["ReadOnlyViolation", "CallRecord", "ReadOnlyGitHubClient", "is_allowed_path"]


class ReadOnlyViolation(Exception):
    """Raised before any network I/O when a request is not a permitted read."""


@dataclass
class CallRecord:
    method: str
    path: str
    status: int | None
    duration_ms: int
    blocked: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- allowlist ------------------------------------------------------------------------------

# GitHub login rules: alphanumeric or hyphen, leading alphanumeric, max 39 (see app/validation.py).
_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
# Repo names: [A-Za-z0-9._-]{1,100}; `.` and `..` rejected in _segments_ok (no lookahead needed).
_REPO = r"[A-Za-z0-9._-]{1,100}"
_NUM = r"[1-9][0-9]{0,9}"
# One path segment of a contents path: unreserved/sub-delim chars or a %XX escape. Dangerous
# escapes (encoded `.`, `/`, `\`, `%`, control chars) are rejected separately.
_SEG = r"(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2})+"
_REPO_PREFIX = rf"/repos/(?P<owner>{_OWNER})/(?P<repo>{_REPO})"

ALLOWED_PATHS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        rf"{_REPO_PREFIX}",                                   # repo metadata
        rf"{_REPO_PREFIX}/pulls/{_NUM}",                      # PR
        rf"{_REPO_PREFIX}/pulls/{_NUM}/files",                # PR files
        rf"{_REPO_PREFIX}/pulls/{_NUM}/commits",              # PR commits
        rf"{_REPO_PREFIX}/pulls/{_NUM}/comments",             # PR review comments
        rf"{_REPO_PREFIX}/issues/{_NUM}/comments",            # issue (conversation) comments
        rf"{_REPO_PREFIX}/contents/(?P<path>{_SEG}(?:/{_SEG})*)",  # file contents
    )
)

_ALLOWED_METHODS = frozenset({"GET", "HEAD"})
_ALLOWED_PARAMS = {
    "ref": re.compile(r"[A-Za-z0-9._/-]{1,250}"),
    "per_page": re.compile(r"[1-9][0-9]{0,2}"),
    "page": re.compile(r"[1-9][0-9]{0,3}"),
}
# Encoded `.` `/` `\` `%` and C0 controls — used for traversal/double-encoding tricks.
_BAD_ESCAPE = re.compile(r"%(?:2[eEfF5]|5[cC]|[01][0-9A-Fa-f]|7[fF])")


def _segments_ok(path: str) -> bool:
    for seg in path.split("/")[1:]:
        if seg in {"", ".", ".."}:
            return False
    return True


def is_allowed_path(path: str) -> bool:
    """True iff ``path`` is a bare, normalised, allowlisted API path."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return False
    if any(c in path for c in "\\?#") or any(ord(c) < 0x21 or ord(c) > 0x7E for c in path):
        return False
    if _BAD_ESCAPE.search(path) or not _segments_ok(path):
        return False
    return any(p.fullmatch(path) for p in ALLOWED_PATHS)


def _params_ok(params: dict[str, Any] | None) -> bool:
    if not params:
        return True
    for k, v in params.items():
        rx = _ALLOWED_PARAMS.get(k)
        if rx is None or not isinstance(v, (str, int)) or isinstance(v, bool):
            return False
        sv = str(v)
        if not rx.fullmatch(sv) or ".." in sv:
            return False
    return True


# --- client ---------------------------------------------------------------------------------

_REDIRECT_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 3


class ReadOnlyGitHubClient:
    def __init__(
        self,
        token: str | None,
        *,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_bytes: int | None = None,
        timeout: float = 30.0,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.github_api_url).rstrip("/")
        self.max_bytes = max_bytes if max_bytes is not None else settings.github_fetch_max_bytes
        self.calls: list[CallRecord] = []
        base = urlsplit(self.base_url)
        self._host = (base.scheme, base.netloc.lower())
        self._base_path = base.path.rstrip("/")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-review-agent",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self) -> "ReadOnlyGitHubClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- guard ---------------------------------------------------------------------------

    def _check(self, method: str, path: str, params: dict[str, Any] | None) -> None:
        if method not in _ALLOWED_METHODS:
            raise ReadOnlyViolation(f"method {method!r} is not permitted (GET/HEAD only)")
        if not is_allowed_path(path):
            raise ReadOnlyViolation(f"path {path!r} is not on the read-only allowlist")
        if not _params_ok(params):
            raise ReadOnlyViolation(f"query parameters {params!r} are not permitted")

    def _record(self, method: str, path: str, status: int | None, t0: float, *, blocked=False, error=None):
        self.calls.append(
            CallRecord(
                method=method,
                path=path,
                status=status,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                blocked=blocked,
                error=error,
            )
        )

    def _redirect_target(self, response: httpx.Response) -> str | None:
        """Path of a same-host redirect target, or None if it leaves the API host."""
        loc = response.headers.get("location")
        if not loc:
            return None
        target = response.request.url.join(loc)
        if (target.scheme, target.netloc.decode().lower()) != self._host:
            return None
        path = target.raw_path.decode("ascii", "replace").split("?", 1)[0]
        if self._base_path:
            if not path.startswith(self._base_path + "/"):
                return None
            path = path[len(self._base_path):]
        return path

    # -- public API ----------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        byte_cap: int | None = None,
    ) -> httpx.Response:
        """Perform one allowlisted read. Raises ``ReadOnlyViolation`` before any I/O.

        ``byte_cap`` streams the body and keeps at most that many bytes
        (``response.extensions["truncated"]`` tells whether it cut anything).
        """
        method = (method or "").upper()
        for _hop in range(_MAX_REDIRECTS + 1):
            t0 = time.perf_counter()
            try:
                self._check(method, path, params)
            except ReadOnlyViolation as exc:
                self._record(method, path, None, t0, blocked=True, error=str(exc))
                raise
            try:
                response = await self._send(method, path, params, headers, byte_cap)
            except ReadOnlyViolation as exc:
                self._record(method, path, None, t0, blocked=True, error=str(exc))
                raise
            except httpx.HTTPError as exc:
                self._record(method, path, None, t0, error=f"{type(exc).__name__}: {exc}")
                raise
            if response.status_code in _REDIRECT_CODES:
                target = self._redirect_target(response)
                if target is None:
                    self._record(method, path, response.status_code, t0,
                                 error="redirect to a foreign host refused")
                    return response
                self._record(method, path, response.status_code, t0, error=f"redirect -> {target}")
                path, params = target, None  # the target is re-checked against the allowlist
                continue
            self._record(method, path, response.status_code, t0)
            return response
        raise httpx.TooManyRedirects("too many redirects", request=response.request)

    async def _send(self, method, path, params, headers, byte_cap) -> httpx.Response:
        req = self._http.build_request(method, self.base_url + path, params=params, headers=headers)
        # Defence in depth: the URL httpx built must still be on our host and path.
        if (req.url.scheme, req.url.netloc.decode().lower()) != self._host:
            raise ReadOnlyViolation("request URL left the API host")
        if byte_cap is None:
            return await self._http.send(req)
        resp = await self._http.send(req, stream=True)
        try:
            buf = bytearray()
            truncated = False
            async for chunk in resp.aiter_bytes():
                room = byte_cap - len(buf)
                if len(chunk) > room:
                    buf.extend(chunk[:max(room, 0)])
                    truncated = True
                    break
                buf.extend(chunk)
        finally:
            await resp.aclose()
        hdrs = [(k, v) for k, v in resp.headers.items()
                if k.lower() not in {"content-length", "content-encoding", "transfer-encoding"}]
        return httpx.Response(
            resp.status_code,
            headers=hdrs,
            content=bytes(buf),
            request=req,
            extensions={"truncated": truncated},
        )

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self.request("GET", path, params)
        response.raise_for_status()
        return response.json()

    async def get_text(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github.raw+json",
    ) -> str:
        """Raw fetch, truncated to ``max_bytes``."""
        response = await self.request(
            "GET", path, params, headers={"Accept": accept}, byte_cap=self.max_bytes
        )
        response.raise_for_status()
        text = response.content.decode("utf-8", errors="replace")
        if response.extensions.get("truncated"):
            text += f"\n[... truncated at {self.max_bytes} bytes ...]"
        return text
