"""Agent tools. Every tool goes through the ReadOnlyGitHubClient and returns repository content
only inside the untrusted-data envelope (see prompts.wrap_untrusted).

Tools are bound to ONE pull request: the model cannot pick another repo or PR, only a path
inside this repo — and even that path is checked by the transport allowlist, not here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

import httpx
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.agent.github_client import ReadOnlyGitHubClient, ReadOnlyViolation
from app.agent.prompts import wrap_untrusted

MAX_FILE_PAGES = 3
PER_PAGE = 100


@dataclass
class PRRef:
    repo: str
    pr_number: int
    head_sha: str | None = None
    base_sha: str | None = None
    changed_files: list[str] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        return f"/repos/{self.repo}"


# --- fetch helpers (also used by the deterministic fetch_context node) ---------------------

async def fetch_pull_request(client: ReadOnlyGitHubClient, ref: PRRef) -> dict[str, Any]:
    return await client.get_json(f"{ref.prefix}/pulls/{ref.pr_number}")


async def fetch_changed_files(client: ReadOnlyGitHubClient, ref: PRRef) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for page in range(1, MAX_FILE_PAGES + 1):
        batch = await client.get_json(
            f"{ref.prefix}/pulls/{ref.pr_number}/files", {"per_page": PER_PAGE, "page": page}
        )
        if not isinstance(batch, list):
            break
        files.extend(batch)
        if len(batch) < PER_PAGE:
            break
    return files


async def fetch_comments(client: ReadOnlyGitHubClient, ref: PRRef) -> tuple[list, list]:
    review = await client.get_json(f"{ref.prefix}/pulls/{ref.pr_number}/comments", {"per_page": PER_PAGE})
    issue = await client.get_json(f"{ref.prefix}/issues/{ref.pr_number}/comments", {"per_page": PER_PAGE})
    return review or [], issue or []


def summarize_pr(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "author": (pr.get("user") or {}).get("login"),
        "base": {"ref": (pr.get("base") or {}).get("ref"), "sha": (pr.get("base") or {}).get("sha")},
        "head": {"ref": (pr.get("head") or {}).get("ref"), "sha": (pr.get("head") or {}).get("sha")},
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changed_files"),
        "body": pr.get("body") or "",
    }


def render_files(files: list[dict[str, Any]], budget: int) -> str:
    parts: list[str] = []
    used = 0
    for f in files:
        patch = f.get("patch") or "(no textual patch: binary or too large)"
        header = f"--- {f.get('filename')} [{f.get('status')}, +{f.get('additions')}/-{f.get('deletions')}]"
        chunk = f"{header}\n{patch}\n"
        if used + len(chunk) > budget:
            parts.append(f"--- {f.get('filename')} [patch omitted: size budget reached; use read_file]\n")
            continue
        used += len(chunk)
        parts.append(chunk)
    return "".join(parts)


def number_lines(text: str) -> str:
    lines = text.split("\n")
    width = len(str(len(lines)))
    return "\n".join(f"{i:>{width}}| {line}" for i, line in enumerate(lines, 1))


def _error(exc: Exception) -> str:
    # Never echo attacker-influenced paths back outside the envelope.
    if isinstance(exc, ReadOnlyViolation):
        return "ERROR: request blocked by the read-only access policy (path or method not permitted)."
    if isinstance(exc, httpx.HTTPStatusError):
        return f"ERROR: GitHub returned HTTP {exc.response.status_code} for this request."
    return f"ERROR: request failed ({type(exc).__name__})."


# --- tool arg schemas ---------------------------------------------------------------------

class _NoArgs(BaseModel):
    pass


class ReadFileArgs(BaseModel):
    path: str = Field(description="Repository-relative file path, e.g. 'src/app/main.py'.")
    ref: Literal["head", "base"] = Field(
        default="head", description="'head' = the PR's version (default), 'base' = before the PR."
    )


def build_tools(client: ReadOnlyGitHubClient, pr: PRRef) -> list[BaseTool]:
    async def get_pull_request() -> str:
        try:
            data = await fetch_pull_request(client, pr)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            return _error(exc)
        return wrap_untrusted(json.dumps(summarize_pr(data), indent=2, ensure_ascii=False),
                              source="get_pull_request")

    async def list_changed_files() -> str:
        try:
            files = await fetch_changed_files(client, pr)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return wrap_untrusted(render_files(files, client.max_bytes), source="list_changed_files")

    async def read_file(path: str, ref: str = "head") -> str:
        sha = pr.base_sha if ref == "base" else pr.head_sha
        params = {"ref": sha} if sha else None
        api_path = f"{pr.prefix}/contents/{quote(path, safe='/')}"
        try:
            text = await client.get_text(api_path, params)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        return wrap_untrusted(number_lines(text), source="read_file", label=f"{path}@{ref}")

    async def list_review_comments() -> str:
        try:
            review, issue = await fetch_comments(client, pr)
        except Exception as exc:  # noqa: BLE001
            return _error(exc)
        data = {
            "review_comments": [
                {"path": c.get("path"), "line": c.get("line"), "author": (c.get("user") or {}).get("login"),
                 "body": c.get("body")}
                for c in review
            ],
            "issue_comments": [
                {"author": (c.get("user") or {}).get("login"), "body": c.get("body")} for c in issue
            ],
        }
        return wrap_untrusted(json.dumps(data, indent=2, ensure_ascii=False), source="list_review_comments")

    return [
        StructuredTool.from_function(
            coroutine=get_pull_request, name="get_pull_request", args_schema=_NoArgs,
            description="Pull request metadata: title, author, base/head refs and SHAs, description.",
        ),
        StructuredTool.from_function(
            coroutine=list_changed_files, name="list_changed_files", args_schema=_NoArgs,
            description="Files changed by the pull request with status, line counts and unified diff patch.",
        ),
        StructuredTool.from_function(
            coroutine=read_file, name="read_file", args_schema=ReadFileArgs,
            description="Full contents of one repository file (line-numbered) at the PR head or base.",
        ),
        StructuredTool.from_function(
            coroutine=list_review_comments, name="list_review_comments", args_schema=_NoArgs,
            description="Existing inline review comments and conversation comments on the pull request.",
        ),
    ]
