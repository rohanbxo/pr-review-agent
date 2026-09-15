"""reverse_apply_patch: eval datasets store head + patch only, and base is derived from them."""

import difflib

import pytest

from app.agent.fixtures import reverse_apply_patch


def _git_style(base: str, head: str) -> str:
    """GitHub-style patch: hunks only, no ---/+++ header, git's no-newline marker."""
    out = []
    for ln in difflib.unified_diff(base.splitlines(keepends=True), head.splitlines(keepends=True), n=3):
        if ln.startswith(("---", "+++")):
            continue
        if not ln.endswith("\n"):
            ln += "\n\\ No newline at end of file\n"
        out.append(ln)
    return "".join(out).rstrip("\n")


BASE = "".join(f"line {i}\n" for i in range(1, 41))


@pytest.mark.parametrize("head", [
    BASE.replace("line 5\n", "line five\n"),                                  # modify
    BASE.replace("line 5\n", "line 5\ninserted\n"),                           # insert
    BASE.replace("line 5\n", ""),                                             # delete
    "new first\n" + BASE,                                                     # insert at top
    BASE.replace("line 1\n", ""),                                             # delete at top
    BASE + "appended\n",                                                      # append
    BASE.replace("line 3\n", "x\n").replace("line 30\n", "y\n"),              # two hunks
    BASE.rstrip("\n"),                                                        # drop final newline
])
def test_reconstructs_base_exactly(head):
    assert reverse_apply_patch(head, _git_style(BASE, head)) == BASE


def test_base_without_trailing_newline():
    base = BASE.rstrip("\n")
    head = base + "\nmore"
    assert reverse_apply_patch(head, _git_style(base, head)) == base


def test_crlf_files_round_trip():
    base = BASE.replace("\n", "\r\n")
    head = base.replace("line 7\r\n", "seven\r\n")
    assert reverse_apply_patch(head, _git_style(base, head)) == base


def test_mismatched_head_is_rejected_not_guessed():
    head = BASE.replace("line 5\n", "line five\n")
    patch = _git_style(BASE, head)
    with pytest.raises(ValueError):
        reverse_apply_patch(head.replace("line 4\n", "tampered\n"), patch)


async def test_mock_transport_serves_derived_base_for_ref_base():
    import base64

    import httpx

    from app.agent.fixtures import mock_transport_for_case

    head = BASE.replace("line 5\n", "line five\n")
    case = {"repo": "acme/widgets", "pr_number": 1, "base_sha": "b" * 40, "head_sha": "h" * 40,
            "files": [{"filename": "x.py", "status": "modified", "patch": _git_style(BASE, head),
                       "content": head}]}
    async with httpx.AsyncClient(transport=mock_transport_for_case(case), base_url="https://api.github.com") as c:
        at_base = (await c.get("/repos/acme/widgets/contents/x.py", params={"ref": "b" * 40})).json()
        at_head = (await c.get("/repos/acme/widgets/contents/x.py", params={"ref": "h" * 40})).json()
    assert base64.b64decode(at_base["content"]).decode() == BASE
    assert base64.b64decode(at_head["content"]).decode() == head
