"""Read-only is enforced at the transport: disallowed requests raise before any I/O."""

import httpx
import pytest

from app.agent.github_client import ReadOnlyGitHubClient, ReadOnlyViolation, is_allowed_path

BASE = "https://api.github.com"


def make_client(seen: list, *, max_bytes: int = 200_000, handler=None):
    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if handler is not None:
            return handler(request)
        return httpx.Response(200, json={"ok": True, "path": request.url.path})

    return ReadOnlyGitHubClient("tok", base_url=BASE, transport=httpx.MockTransport(_handler), max_bytes=max_bytes)


ALLOWED = [
    "/repos/psf/requests",
    "/repos/psf/requests/pulls/6093",
    "/repos/psf/requests/pulls/6093/files",
    "/repos/psf/requests/pulls/6093/commits",
    "/repos/psf/requests/pulls/6093/comments",
    "/repos/psf/requests/issues/6093/comments",
    "/repos/psf/requests/contents/src/requests/models.py",
    "/repos/octo-org/.github/contents/README.md",
    "/repos/a/b.c_d-e/contents/docs/my%20file.md",
]

DENIED = [
    "/user",
    "/user/emails",
    "/user/orgs",
    "/repos/o/r/hooks",
    "/repos/o/r/collaborators/bob/permission",
    "/repos/o/r/pulls/1/merge",
    "/repos/o/r/pulls/1/reviews",
    "/repos/o/r/issues/1",
    "/repos/o/r/git/refs",
    "/app/installations/1/access_tokens",
    "/repos/o/r/contents/../../x",
    "/repos/o/r/contents/../hooks",
    "/repos/o/r/contents/src/../../../etc/passwd",
    "/repos/o/r/contents/./x",
    "/repos/o/r/contents/%2e%2e/%2e%2e/etc/passwd",
    "/repos/o/r/contents/%2E%2E%2Fhooks",
    "/repos/o/r/contents/a%2fb",
    "/repos/o/r/contents/a%5c..%5cb",
    "/repos/o/r/contents/%252e%252e/x",
    "/repos/o/r/contents/a\\..\\b",
    "/repos/o/r/contents/",
    "/repos/o/r/contents//etc/passwd",
    "/repos/o/r/contents/x%00y",
    "/repos/o/r/pulls/1?x=/hooks",
    "/repos/o/r/pulls/1#frag",
    "/repos/o/r/pulls/0",
    "/repos/o/r/pulls/1/",
    "/repos/-bad/r",
    "/repos/o/../hooks",
    "/repos/o/./pulls/1",
    "/repos/o/../../user",
    "/repos/o/r/../../user",
    "https://evil.example.com/repos/o/r",
    "//evil.example.com/repos/o/r",
    "http://api.github.com/repos/o/r",
    "repos/o/r",
    "/repos/o/r/contents/a b",
    "/repos/o/r/contents/café.py",
    "",
]


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE", "OPTIONS", "TRACE", "CONNECT"])
async def test_mutating_methods_raise_before_io(method):
    seen: list = []
    async with make_client(seen) as gh:
        with pytest.raises(ReadOnlyViolation):
            await gh.request(method, "/repos/o/r/pulls/1")
        assert seen == []
        assert len(gh.calls) == 1
        rec = gh.calls[0]
        assert rec.blocked and rec.method == method and rec.status is None and rec.error


@pytest.mark.parametrize("path", DENIED)
async def test_off_allowlist_get_raises_and_is_recorded(path):
    seen: list = []
    async with make_client(seen) as gh:
        with pytest.raises(ReadOnlyViolation):
            await gh.request("GET", path)
        with pytest.raises(ReadOnlyViolation):
            await gh.get_json(path)
        with pytest.raises(ReadOnlyViolation):
            await gh.get_text(path)
    assert seen == [], "a blocked request must never reach the transport"
    assert len(gh.calls) == 3 and all(c.blocked for c in gh.calls)
    assert not is_allowed_path(path)


@pytest.mark.parametrize("path", ALLOWED)
async def test_on_allowlist_get_is_permitted_and_recorded(path):
    seen: list = []
    async with make_client(seen) as gh:
        data = await gh.get_json(path)
        head = await gh.request("HEAD", path)
    assert data["ok"] is True
    assert head.status_code == 200
    assert [r.method for r in seen] == ["GET", "HEAD"]
    assert seen[0].url.host == "api.github.com"
    assert [(c.method, c.path, c.status, c.blocked) for c in gh.calls] == [
        ("GET", path, 200, False), ("HEAD", path, 200, False)
    ]
    assert all(isinstance(c.duration_ms, int) and c.duration_ms >= 0 for c in gh.calls)
    assert set(gh.calls[0].as_dict()) == {"method", "path", "status", "duration_ms", "blocked", "error"}


async def test_lowercase_method_normalised_and_auth_header_sent():
    seen: list = []
    async with make_client(seen) as gh:
        await gh.request("get", "/repos/o/r")
    assert seen[0].method == "GET"
    assert seen[0].headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize("params", [
    {"q": "x"},
    {"ref": "../../etc"},
    {"ref": "main&access_token=x"},
    {"per_page": "100; DROP"},
    {"page": True},
    {"ref": ["a", "b"]},
])
async def test_query_string_smuggling_blocked(params):
    seen: list = []
    async with make_client(seen) as gh:
        with pytest.raises(ReadOnlyViolation):
            await gh.request("GET", "/repos/o/r/contents/a.py", params)
    assert seen == [] and gh.calls[0].blocked


async def test_allowed_params_pass():
    seen: list = []
    async with make_client(seen) as gh:
        await gh.get_json("/repos/o/r/pulls/1/files", {"per_page": 100, "page": 2})
        await gh.get_json("/repos/o/r/contents/a.py", {"ref": "feature/x-1.2"})
    assert seen[0].url.params["per_page"] == "100"
    assert seen[1].url.params["ref"] == "feature/x-1.2"


async def test_truncation_to_byte_cap():
    seen: list = []
    body = b"A" * 5000

    def handler(request):
        return httpx.Response(200, content=body)

    async with make_client(seen, max_bytes=1024, handler=handler) as gh:
        text = await gh.get_text("/repos/o/r/contents/big.txt")
        resp = await gh.request("GET", "/repos/o/r/contents/big.txt", byte_cap=1024)
    assert text.startswith("A" * 1024)
    assert text.count("A") == 1024
    assert "truncated" in text
    assert len(resp.content) == 1024 and resp.extensions["truncated"] is True
    assert gh.calls[0].status == 200 and not gh.calls[0].blocked


async def test_small_body_not_marked_truncated():
    seen: list = []
    async with make_client(seen, max_bytes=1024, handler=lambda r: httpx.Response(200, text="hello")) as gh:
        assert await gh.get_text("/repos/o/r/contents/x.txt") == "hello"


async def test_redirect_to_other_host_not_followed():
    seen: list = []

    def handler(request):
        return httpx.Response(302, headers={"location": "https://evil.example.com/repos/o/r"})

    async with make_client(seen, handler=handler) as gh:
        with pytest.raises(httpx.HTTPStatusError):
            await gh.get_json("/repos/o/r")
    assert len(seen) == 1 and seen[0].url.host == "api.github.com"
    assert gh.calls[0].status == 302 and "foreign host" in gh.calls[0].error


async def test_same_host_redirect_off_allowlist_is_blocked():
    seen: list = []

    def handler(request):
        return httpx.Response(301, headers={"location": "https://api.github.com/repos/o/r/hooks"})

    async with make_client(seen, handler=handler) as gh:
        with pytest.raises(ReadOnlyViolation):
            await gh.get_json("/repos/o/r")
    assert len(seen) == 1
    assert gh.calls[-1].blocked and gh.calls[-1].path == "/repos/o/r/hooks"


async def test_same_host_redirect_on_allowlist_is_followed():
    seen: list = []

    def handler(request):
        if request.url.path == "/repos/old/r":
            return httpx.Response(301, headers={"location": "/repos/new/r"})
        return httpx.Response(200, json={"full_name": "new/r"})

    async with make_client(seen, handler=handler) as gh:
        data = await gh.get_json("/repos/old/r")
    assert data["full_name"] == "new/r"
    assert [c.path for c in gh.calls] == ["/repos/old/r", "/repos/new/r"]


async def test_mixed_log_preserves_order():
    seen: list = []
    async with make_client(seen) as gh:
        await gh.get_json("/repos/o/r")
        with pytest.raises(ReadOnlyViolation):
            await gh.request("POST", "/repos/o/r/pulls/1/reviews")
        await gh.get_json("/repos/o/r/pulls/1")
    assert [(c.method, c.blocked) for c in gh.calls] == [("GET", False), ("POST", True), ("GET", False)]
    assert len(seen) == 2
