"""Serve a dataset case (CONTRACTS.md § Dataset case format) as the GitHub REST API.

Eval and the injection tests run the REAL ReadOnlyGitHubClient (allowlist included) over this
transport, so the agent sees exactly the paths and shapes it would see against api.github.com.
Unknown paths return 404; non-GET/HEAD return 405 (the client should never let one through).
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import unquote

import httpx

_FAKE_BASE_SHA = "0" * 40
_FAKE_HEAD_SHA = "f" * 40


def _json(status: int, data: Any) -> httpx.Response:
    return httpx.Response(status, json=data)


def _not_found() -> httpx.Response:
    return _json(404, {"message": "Not Found", "documentation_url": "https://docs.github.com/rest"})


def mock_transport_for_case(case: dict, *, seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    """Build a MockTransport for ``case``. If ``seen`` is given, every request that reaches the
    transport is appended to it (tests use this to prove blocked calls never got here)."""
    repo: str = case["repo"]
    number = int(case["pr_number"])
    base_sha = case.get("base_sha") or _FAKE_BASE_SHA
    head_sha = case.get("head_sha") or _FAKE_HEAD_SHA
    files: list[dict] = case.get("files") or []
    author = case.get("author") or "contributor"

    head_contents: dict[str, str] = {f["filename"]: f["content"] for f in files if f.get("content") is not None}
    head_contents.update(case.get("repo_files") or {})  # optional: unchanged files readable at head
    base_contents: dict[str, str] = {f["filename"]: f["base_content"] for f in files if f.get("base_content") is not None}

    owner, name = repo.split("/")
    prefix = f"/repos/{owner}/{name}"
    rx_contents = re.compile(re.escape(prefix) + r"/contents/(.+)")

    pr_json = {
        "number": number,
        "title": case.get("title", ""),
        "body": case.get("body", ""),
        "state": "open",
        "user": {"login": author},
        "base": {"ref": "main", "sha": base_sha, "repo": {"full_name": repo}},
        "head": {"ref": f"pr-{number}", "sha": head_sha, "repo": {"full_name": repo}},
        "additions": sum(int(f.get("additions") or 0) for f in files),
        "deletions": sum(int(f.get("deletions") or 0) for f in files),
        "changed_files": len(files),
    }
    files_json = [
        {
            "sha": f"{i:040x}",
            "filename": f["filename"],
            "status": f.get("status", "modified"),
            "additions": int(f.get("additions") or 0),
            "deletions": int(f.get("deletions") or 0),
            "changes": int(f.get("additions") or 0) + int(f.get("deletions") or 0),
            **({"patch": f["patch"]} if f.get("patch") is not None else {}),
        }
        for i, f in enumerate(files, 1)
    ]
    review_comments = [
        {"id": 1000 + i, "path": c.get("path"), "line": c.get("line"), "body": c.get("body", ""),
         "user": {"login": c.get("author", "reviewer")}, "commit_id": head_sha}
        for i, c in enumerate(case.get("review_comments") or [])
    ]
    issue_comments = [
        {"id": 2000 + i, "body": c.get("body", ""), "user": {"login": c.get("author", "commenter")}}
        for i, c in enumerate(case.get("issue_comments") or [])
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.method not in {"GET", "HEAD"}:
            return _json(405, {"message": "Method Not Allowed"})
        path = request.url.raw_path.decode("ascii", "replace").split("?", 1)[0]
        page = int(request.url.params.get("page", "1") or 1)

        if path == prefix:
            return _json(200, {"full_name": repo, "private": False, "default_branch": "main"})
        if path == f"{prefix}/pulls/{number}":
            return _json(200, pr_json)
        if path == f"{prefix}/pulls/{number}/files":
            return _json(200, files_json if page == 1 else [])
        if path == f"{prefix}/pulls/{number}/commits":
            return _json(200, [{"sha": head_sha, "commit": {"message": case.get("title", "")}}] if page == 1 else [])
        if path == f"{prefix}/pulls/{number}/comments":
            return _json(200, review_comments if page == 1 else [])
        if path == f"{prefix}/issues/{number}/comments":
            return _json(200, issue_comments if page == 1 else [])

        m = rx_contents.fullmatch(path)
        if m:
            file_path = unquote(m.group(1))
            ref = request.url.params.get("ref")
            source = base_contents if ref is not None and ref == base_sha and ref != head_sha else head_contents
            if file_path not in source:
                return _not_found()
            text = source[file_path]
            accept = request.headers.get("accept", "")
            if "raw" in accept:
                return httpx.Response(200, content=text.encode("utf-8"),
                                      headers={"content-type": "text/plain; charset=utf-8"})
            raw = text.encode("utf-8")
            return _json(200, {
                "type": "file", "encoding": "base64", "name": file_path.rsplit("/", 1)[-1],
                "path": file_path, "size": len(raw), "sha": head_sha,
                "content": base64.encodebytes(raw).decode("ascii"),
            })
        return _not_found()

    return httpx.MockTransport(handler)


def load_cases(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
