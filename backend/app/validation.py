"""Repo name validation.

pydantic v2 compiles `pattern=` with Rust's regex crate (no lookahead), so rules like
"not `.` or `..`" live here and are called from field_validators.
"""

import re

# GitHub login rules: alphanumeric or hyphen, leading alphanumeric, max 39.
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
# Repos may start with a dot (.github is real); `.` and `..` are rejected separately.
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")


def validate_repo_full_name(value: str, *, allow_wildcard: bool = False) -> str:
    if not isinstance(value, str) or value.count("/") != 1:
        raise ValueError("repo must be 'owner/name'")
    owner, repo = value.split("/")
    if not _OWNER.fullmatch(owner):
        raise ValueError("invalid owner")
    if repo == "*":
        if not allow_wildcard:
            raise ValueError("wildcard not allowed here")
        return value
    if repo in {".", ".."} or not _REPO.fullmatch(repo):
        raise ValueError("invalid repository name")
    return value


def grant_covers(grant: str, repo_full_name: str) -> bool:
    """Case-insensitive, as GitHub names are."""
    g_owner, g_repo = grant.lower().split("/")
    owner, repo = repo_full_name.lower().split("/")
    return g_owner == owner and (g_repo == "*" or g_repo == repo)
