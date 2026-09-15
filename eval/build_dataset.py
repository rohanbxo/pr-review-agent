"""Build the eval dataset from the git history of public repositories.

    python -m eval.build_dataset                 # uses pinned refs from eval/data/manifest.json
    python -m eval.build_dataset --refresh       # fetch clones, re-pin to current default branch

Nothing here talks to the GitHub API: repositories are cloned over HTTPS (bare) into
``eval/.cache/repos`` and everything -- PR number, title, body, per-file patches, head and base
file contents -- is read from git objects. See ``eval/data/README.md`` for provenance and caveats.

Splits (CONTRACTS.md § Dataset case format):

* ``injected`` -- a real merged PR with one linter-surviving mutation (``eval/mutations.py``) on a
  line the PR added; balanced across the four bug kinds.
* ``reverted`` -- a real bug-fix commit, inverted: the "PR" takes the fixed file forward through
  real later development and re-introduces the bug inside it (see ``pad_revert``).
* ``clean``    -- real merged PRs, untouched.
* ``v1``       -- concatenation of the three.

Deterministic: pinned commit SHAs per repo, sorted candidate pools, string-seeded RNGs.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import random
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from eval import mutations as mut

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache" / "repos"
DATA = ROOT / "data"
MANIFEST = DATA / "manifest.json"

REPOS = [
    "psf/requests",
    "pallets/flask",
    "pallets/click",
    "encode/httpx",
    "python-attrs/attrs",
    "pypa/packaging",
    "tiangolo/typer",
    "marshmallow-code/marshmallow",
    "pallets/werkzeug",
    "pallets/jinja",
    # added because `range(len(...))` is rare in modern code: these supply off-by-one sites
    "more-itertools/more-itertools",
    "dateutil/dateutil",
    "Textualize/rich",
    "pyparsing/pyparsing",
    "networkx/networkx",
]

TARGETS = {"injected": 40, "reverted": 25, "clean": 150}
SEED = "pr-review-eval-v1"

MAX_FILES = 8
MAX_CHANGED_LINES = 600
MAX_FILE_BYTES = 200_000  # == settings.github_fetch_max_bytes: never hand the agent a truncated file
DOC_EXTS = {".rst", ".md", ".txt"}
VENDORED = re.compile(r"(^|/)(_?vendor(ed)?|extern|third_party|node_modules)/|_pb2\.py$|(^|/)requests/packages/")

MERGE_RE = re.compile(r"^Merge pull request #(\d+) from (\S+)")
SQUASH_RE = re.compile(r"^(.*\S)\s*\(#(\d+)\)\s*$")
SKIP_TITLE = re.compile(r"^(revert|bump |\[pre-commit\.ci\]|pre-commit|release |prepare release|"
                        r"update (dev )?(requirements|dependencies)|merge branch)", re.I)
FIX_RE = re.compile(r"\b(fix(es|ed)?|bug(fix)?|regression)\b", re.I)
FIX_EXCLUDE = re.compile(r"typo|\bdocs?\b|documentation|docstring|changelog|lint|flake8|mypy|pyright|"
                         r"typing|type hints?|annotation|pre-commit|\bci\b|coverage|warning|deprecat|"
                         r"readme|spelling|grammar|format|black|isort|ruff|\btests?\b only|revert", re.I)


# --------------------------------------------------------------------------- git plumbing
def git(repo_dir: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(repo_dir), *args],
                          check=True, capture_output=True).stdout


class BlobReader:
    """Persistent ``git cat-file --batch`` for fast ``<rev>:<path>`` reads."""

    def __init__(self, repo_dir: Path) -> None:
        self.proc = subprocess.Popen(["git", "-C", str(repo_dir), "cat-file", "--batch"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def read(self, rev: str, path: str) -> bytes | None:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(f"{rev}:{path}\n".encode("utf-8"))
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().decode("utf-8", "replace").split()
        if len(header) < 3 or header[-1] == "missing":
            return None
        size = int(header[2])
        data = self.proc.stdout.read(size)
        self.proc.stdout.read(1)
        return data if header[1] == "blob" else None

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait()


def repo_dir(repo: str) -> Path:
    return CACHE / repo.replace("/", "_")


def ensure_clone(repo: str, refresh: bool) -> None:
    d = repo_dir(repo)
    if not d.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        print(f"  cloning {repo} ...", file=sys.stderr)
        subprocess.run(["git", "clone", "--quiet", "--bare", f"https://github.com/{repo}.git", str(d)],
                       check=True)
    elif refresh:
        subprocess.run(["git", "-C", str(d), "fetch", "--quiet", "origin", "+refs/heads/*:refs/heads/*"],
                       check=True)


# --------------------------------------------------------------------------- commit scanning
@dataclass
class Commit:
    repo: str
    sha: str
    parents: list[str]
    timestamp: int
    author: str
    message: str
    numstat: list[tuple[int | None, int | None, str]] = field(default_factory=list)

    @property
    def subject(self) -> str:
        return self.message.split("\n", 1)[0].strip()


def scan_commits(repo: str, ref: str) -> list[Commit]:
    out = git(repo_dir(repo), "log", "--first-parent", "--diff-merges=first-parent", "--no-renames",
              "--numstat", "--format=%x1e%H%x1f%P%x1f%ct%x1f%an%x1f%B%x1f", ref)
    commits = []
    for chunk in out.decode("utf-8", "replace").split("\x1e")[1:]:
        sha, parents, ts, author, message, rest = chunk.split("\x1f", 5)
        c = Commit(repo, sha, parents.split(), int(ts), author, message.strip())
        for line in rest.strip().splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            a, d, path = parts
            c.numstat.append((None if a == "-" else int(a), None if d == "-" else int(d), path))
        commits.append(c)
    return commits


def is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (bool(re.search(r"(^|/)(tests?|testing)/", path)) or name.startswith("test_")
            or name.endswith("_test.py") or name == "conftest.py")


def is_source_py(path: str) -> bool:
    return (path.endswith(".py") and not is_test_path(path) and not VENDORED.search(path)
            and not re.match(r"(docs?|examples?|scripts?|benchmarks?)/", path))


def pr_info(c: Commit) -> tuple[int, str, str] | None:
    """(pr_number, title, body) if this first-parent commit is a merged PR."""
    subject = c.subject
    rest = c.message.split("\n", 1)[1].strip() if "\n" in c.message else ""
    m = MERGE_RE.match(subject)
    if m and len(c.parents) == 2:
        lines = rest.split("\n", 1)
        title = lines[0].strip() or subject
        body = lines[1].strip() if len(lines) > 1 else ""
        return int(m.group(1)), title, body
    m = SQUASH_RE.match(subject)
    if m and len(c.parents) == 1:
        return int(m.group(2)), m.group(1), rest
    return None


def reviewable(c: Commit) -> bool:
    if not c.numstat or len(c.numstat) > MAX_FILES:
        return False
    if "[bot]" in c.author or "pre-commit-ci" in c.author:
        return False
    total = 0
    for a, d, path in c.numstat:
        if a is None or d is None or VENDORED.search(path) or path.startswith('"'):
            return False
        ext = Path(path).suffix
        if ext != ".py" and ext not in DOC_EXTS:
            return False
        total += a + d
    return 0 < total <= MAX_CHANGED_LINES and any(is_source_py(p) for _, _, p in c.numstat)


# --------------------------------------------------------------------------- diff → case files
def parse_diff(raw: str) -> dict[str, dict]:
    """``git diff`` output → {path: {status, patch}} (hunks only, GitHub style)."""
    files: dict[str, dict] = {}
    for block in re.split(r"^diff --git ", raw, flags=re.M)[1:]:
        lines = block.split("\n")
        status, old, new = "modified", None, None
        i = 1
        while i < len(lines) and not lines[i].startswith("@@"):
            ln = lines[i]
            if ln.startswith("new file mode"):
                status = "added"
            elif ln.startswith("deleted file mode"):
                status = "removed"
            elif ln.startswith("--- "):
                old = None if ln[4:] == "/dev/null" else ln[6:]
            elif ln.startswith("+++ "):
                new = None if ln[4:] == "/dev/null" else ln[6:]
            elif ln.startswith("Binary files"):
                raise ValueError("binary")
            i += 1
        path = new or old
        if path is None:
            continue  # mode-only change
        patch = "\n".join(lines[i:]).rstrip("\n")
        files[path] = {"status": status, "patch": patch}
    return files


def strip_docstrings(tree: ast.AST) -> str:
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree)


def load_files(repo: str, base: str, head: str, blobs: BlobReader) -> list[dict] | None:
    try:
        raw = git(repo_dir(repo), "diff", "--no-renames", "--no-color", "-U3", base, head)
        parsed = parse_diff(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    out = []
    for path in sorted(parsed):
        info = parsed[path]
        adds, dels = mut.count_changes(info["patch"])
        entry = {"filename": path, "status": info["status"], "additions": adds, "deletions": dels,
                 "patch": info["patch"]}
        try:
            if info["status"] != "removed":
                data = blobs.read(head, path)
                if data is None or len(data) > MAX_FILE_BYTES:
                    return None
                entry["content"] = data.decode("utf-8")
            if info["status"] != "added":
                data = blobs.read(base, path)
                if data is None or len(data) > MAX_FILE_BYTES:
                    return None
                entry["base_content"] = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        out.append(entry)
    return out or None


def base_case(c: Commit, split: str, number: int, title: str, body: str, files: list[dict],
              base: str, head: str) -> dict:
    return {
        "id": "",
        "split": split,
        "repo": c.repo,
        "pr_number": number,
        "title": title,
        "body": body,
        "author": "contributor",
        "base_sha": base,
        "head_sha": head,
        "files": files,
        "review_comments": [],
        "issue_comments": [],
        "expected": None,
        "source": {"repo_url": f"https://github.com/{c.repo}", "commit": c.sha, "parents": c.parents,
                   "committed_at": c.timestamp},
    }


def interleave(groups: dict[str, list]) -> list:
    """Round-robin across repos (sorted), preserving each group's order."""
    keys = sorted(groups)
    out, i = [], 0
    while any(i < len(groups[k]) for k in keys):
        for k in keys:
            if i < len(groups[k]):
                out.append(groups[k][i])
        i += 1
    return out


# --------------------------------------------------------------------------- splits
def build_injected(pool: dict[str, list[Commit]], blobs: dict[str, BlobReader], target: int
                   ) -> tuple[list[dict], set[str]]:
    per_kind = {k: target // len(mut.KINDS) + (1 if i < target % len(mut.KINDS) else 0)
                for i, k in enumerate(mut.KINDS)}
    got: dict[str, list[dict]] = {k: [] for k in mut.KINDS}
    used: set[str] = set()
    order = {}
    for repo, commits in pool.items():
        cs = list(commits)
        random.Random(f"{SEED}:injected:{repo}").shuffle(cs)
        order[repo] = cs
    candidates = interleave(order)

    # Pass 1 finds sites for the rare kinds first; pass 2 fills the rest. Within a pass a PR is
    # assigned to the kind with the largest remaining deficit that it supports.
    passes = [("off_by_one_range_len", "removed_none_guard"), mut.KINDS, None]
    for kinds_this_pass in passes:
        if kinds_this_pass is None:
            # Pass 3: a kind whose sites ran out (in practice off_by_one_range_len -- modern code
            # rarely writes range(len(x))) hands its remaining slots to the other kinds, so the
            # split still reaches its size. The imbalance is recorded in the manifest.
            short = sum(per_kind[k] - len(got[k]) for k in mut.KINDS)
            exhausted = [k for k in mut.KINDS if len(got[k]) < per_kind[k]]
            others = [k for k in mut.KINDS if k not in exhausted]
            if not short or not others:
                break
            for k in exhausted:
                per_kind[k] = len(got[k])
            for i in range(short):
                per_kind[others[i % len(others)]] += 1
            kinds_this_pass = tuple(others)
        for c in candidates:
            if all(len(got[k]) >= per_kind[k] for k in kinds_this_pass):
                break
            if c.sha in used:
                continue
            info = pr_info(c)
            assert info
            files = None
            wanted = sorted((k for k in kinds_this_pass if len(got[k]) < per_kind[k]),
                            key=lambda k: (len(got[k]) - per_kind[k], mut.KINDS.index(k)))
            # cheap text prefilter before touching blobs
            for kind in wanted:
                if kind == "off_by_one_range_len" and (
                        c.sha not in _range_len_shas(c.repo) or "range(len(" not in _added_text(c)):
                    continue
                if kind == "removed_none_guard" and "is None" not in _added_text(c) \
                        and "is not None" not in _added_text(c):
                    continue
                if files is None:
                    files = load_files(c.repo, c.parents[0], c.sha, blobs[c.repo])
                    if files is None:
                        break
                rng = random.Random(f"{SEED}:mutate:{c.sha}:{kind}")
                case = _inject(c, info, files, kind, rng)
                if case is not None:
                    got[kind].append(case)
                    used.add(c.sha)
                    print(f"  injected {kind:22s} {c.repo}#{info[0]} "
                          f"({sum(len(v) for v in got.values())}/{target})", file=sys.stderr)
                    break
    cases = [case for k in mut.KINDS for case in got[k]]
    return cases, used


_ADDED_TEXT_CACHE: dict[str, str] = {}
_RANGE_LEN_CACHE: dict[str, set[str]] = {}


def _range_len_shas(repo: str) -> set[str]:
    """Commits whose first-parent diff adds/removes `range(len(` (one pickaxe pass per repo)."""
    if repo not in _RANGE_LEN_CACHE:
        out = git(repo_dir(repo), "log", "--first-parent", "--diff-merges=first-parent", "--format=%H", "-s",
                  r"-Grange\(len\(", "HEAD", "--", "*.py")
        _RANGE_LEN_CACHE[repo] = set(out.decode().split())
    return _RANGE_LEN_CACHE[repo]


def _added_text(c: Commit) -> str:
    if c.sha not in _ADDED_TEXT_CACHE:
        raw = git(repo_dir(c.repo), "diff", "--no-renames", "-U0", c.parents[0], c.sha, "--", "*.py")
        _ADDED_TEXT_CACHE[c.sha] = "\n".join(
            ln for ln in raw.decode("utf-8", "replace").splitlines() if ln.startswith("+"))
    return _ADDED_TEXT_CACHE[c.sha]


def _inject(c: Commit, info, files: list[dict], kind: str, rng: random.Random) -> dict | None:
    number, title, body = info
    targets = [f for f in files if is_source_py(f["filename"]) and f.get("content") is not None]
    rng.shuffle(targets)
    for f in targets:
        allowed = mut.added_lines_from_patch(f["patch"])
        if not allowed:
            continue
        m = mut.mutate(kind, f["content"], allowed, rng)
        if m is None:
            continue
        new_files = []
        for g in files:
            g = dict(g)
            if g["filename"] == f["filename"]:
                g["content"] = m.source
                g["patch"] = mut.unified_patch(g.get("base_content") or "", m.source)
                g["additions"], g["deletions"] = mut.count_changes(g["patch"])
                # The bug must sit inside the reviewed change. A flip that happens to restore the
                # base text would drop out of the diff; a removed guard leaves no + line, so it
                # only needs to be next to the change.
                slack = 3 if kind == "removed_none_guard" else 0
                bug = set(range(m.lines["start"] - slack, m.lines["end"] + slack + 1))
                if not bug & touched_head_lines(g["patch"]):
                    return None
            new_files.append(g)
        case = base_case(c, "injected", number, title, body, new_files, c.parents[0], c.sha)
        case["id"] = f"{c.repo.split('/')[1]}-{number}-{kind}"
        case["expected"] = {"bug_kind": kind, "file": f["filename"], "lines": m.lines}
        case["source"]["mutation"] = m.description
        return case
    return None


def _deletion_points(patch: str) -> set[int]:
    """Head lines adjacent to pure deletions (where removed code used to be)."""
    points: set[int] = set()
    new_line = 0
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            new_line = int(m.group(1)) if m else new_line
        elif raw.startswith("+"):
            new_line += 1
        elif raw.startswith("-"):
            points.update({max(1, new_line - 1), max(1, new_line)})
        elif not raw.startswith("\\"):
            new_line += 1
    return points


def touched_head_lines(patch: str) -> set[int]:
    return mut.added_lines_from_patch(patch) | _deletion_points(patch)


PAD_MIN_LINES, PAD_MAX_LINES, PAD_MAX_COMMITS = 10, 300, 40


def revert_fix_onto(buggy_s: str, fixed_s: str, later_s: str, context: int = 2) -> str | None:
    """Undo the fix (buggy -> fixed) inside a LATER version of the file.

    Every changed block of the fix, with ``context`` equal lines either side, must occur exactly
    once in ``later_s``; it is replaced by the pre-fix block. ``None`` if any block moved on."""
    import difflib

    buggy, fixed, later = mut.split_lines(buggy_s), mut.split_lines(fixed_s), mut.split_lines(later_s)
    edits = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, buggy, fixed, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        lo, hi = max(0, j1 - context), min(len(fixed), j2 + context)
        needle = fixed[lo:hi]
        replacement = fixed[lo:j1] + buggy[i1:i2] + fixed[j2:hi]
        if not needle:
            return None
        hits = [k for k in range(len(later) - len(needle) + 1) if later[k:k + len(needle)] == needle]
        if len(hits) != 1:
            return None
        edits.append((hits[0], hits[0] + len(needle), replacement))
    edits.sort()
    if not edits or any(a[1] > b[0] for a, b in zip(edits, edits[1:])):
        return None
    for start, end, replacement in reversed(edits):
        later[start:end] = replacement
    return "".join(later)


def pad_revert(c: Commit, path: str, fixed_s: str, buggy_s: str, blobs: BlobReader, pin: str
               ) -> tuple[str, str, set[int]] | None:
    """Bundle the inverted fix with real later development of the same file.

    A bare inverted fix is a diff of a few lines -- "flag the first hunk" localises it for free.
    Instead take the file at a later first-parent commit L (F..L changed PAD_MIN..PAD_MAX lines),
    undo the fix inside L, and present F -> that as the PR: realistic surrounding changes plus a
    regression. Returns (head content, L sha, head lines of the regression)."""
    out = git(repo_dir(c.repo), "log", "--first-parent", "--reverse", "--format=%H", f"{c.sha}..{pin}", "--", path)
    for later_sha in out.decode().split()[:PAD_MAX_COMMITS]:
        data = blobs.read(later_sha, path)
        if data is None or len(data) > MAX_FILE_BYTES:
            return None
        try:
            later_s = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        pad = sum(mut.count_changes(mut.unified_patch(fixed_s, later_s)))
        if pad < PAD_MIN_LINES:
            continue
        if pad > PAD_MAX_LINES:
            return None
        head_s = revert_fix_onto(buggy_s, fixed_s, later_s)
        if head_s is None:
            return None  # the fixed code has moved on; later commits will not bring it back
        try:
            if ast.dump(ast.parse(head_s)) == ast.dump(ast.parse(later_s)):
                return None
            if mut.pyflakes_messages(head_s) - mut.pyflakes_messages(later_s):
                return None  # e.g. an import only the fix used would be left unused: a giveaway
        except (SyntaxError, ValueError):
            return None
        bug = touched_head_lines(mut.unified_patch(later_s, head_s))
        if not bug or max(bug) - min(bug) > 40:
            return None
        return head_s, later_sha, bug
    return None


def build_reverted(all_commits: dict[str, list[Commit]], blobs: dict[str, BlobReader], target: int,
                   exclude: set[str], pins: dict[str, str]) -> list[dict]:
    with_test: dict[str, list[Commit]] = {}
    without_test: dict[str, list[Commit]] = {}
    for repo, commits in all_commits.items():
        for c in commits:
            if c.sha in exclude or len(c.parents) != 1 or not c.numstat:
                continue
            if not FIX_RE.search(c.subject) or FIX_EXCLUDE.search(c.subject):
                continue
            if any(a is None for a, _, _ in c.numstat) or len(c.numstat) > MAX_FILES:
                continue
            src = [(a, d, p) for a, d, p in c.numstat if is_source_py(p)]
            if len(src) != 1 or not (1 <= src[0][0] + src[0][1] <= 40):
                continue
            if any(p.endswith(".py") and not is_source_py(p) and not is_test_path(p)
                   for _, _, p in c.numstat):
                continue
            has_test = any(is_test_path(p) for _, _, p in c.numstat)
            (with_test if has_test else without_test).setdefault(repo, []).append(c)
    ordered = []
    for groups in (with_test, without_test):
        for repo in groups:
            random.Random(f"{SEED}:reverted:{repo}").shuffle(groups[repo])
        ordered.extend(interleave(groups))

    cases: list[dict] = []
    per_repo: dict[str, int] = {}
    cap = max(3, -(-target // max(1, len(all_commits))) + 1)
    for c in ordered:
        if len(cases) >= target:
            break
        if per_repo.get(c.repo, 0) >= cap:
            continue
        src_path = next(p for _, _, p in c.numstat if is_source_py(p))
        fixed = blobs[c.repo].read(c.sha, src_path)
        buggy = blobs[c.repo].read(c.parents[0], src_path)
        if fixed is None or buggy is None or max(len(fixed), len(buggy)) > MAX_FILE_BYTES:
            continue
        try:
            fixed_s, buggy_s = fixed.decode("utf-8"), buggy.decode("utf-8")
            if strip_docstrings(ast.parse(fixed_s)) == strip_docstrings(ast.parse(buggy_s)):
                continue  # docs/comments-only "fix"
        except (UnicodeDecodeError, SyntaxError, ValueError):
            continue
        padded = pad_revert(c, src_path, fixed_s, buggy_s, blobs[c.repo], pins[c.repo])
        if padded is None:
            continue
        head_s, later_sha, bug = padded
        patch = mut.unified_patch(fixed_s, head_s)
        adds, dels = mut.count_changes(patch)
        number = 100000 + int(c.sha[:8], 16) % 800000  # synthetic: this "PR" never existed
        files = [{"filename": src_path, "status": "modified", "additions": adds, "deletions": dels,
                  "patch": patch, "content": head_s, "base_content": fixed_s}]
        # Neutral title: the fix commit's message would give the answer away.
        case = base_case(c, "reverted", number, f"Update {src_path.rsplit('/', 1)[-1]}", "",
                         files, c.sha, later_sha)
        case["id"] = f"{c.repo.split('/')[1]}-{c.sha[:10]}-reverted"
        case["expected"] = {"bug_kind": "reverted_fix", "file": src_path,
                            "lines": {"start": min(bug), "end": max(bug)}}
        case["source"].update({"fix_commit": c.sha, "fix_subject": c.subject, "inverted": True,
                               "padding_commit": later_sha,
                               "fix_touched_tests": any(is_test_path(p) for _, _, p in c.numstat)})
        cases.append(case)
        per_repo[c.repo] = per_repo.get(c.repo, 0) + 1
        print(f"  reverted {c.repo} {c.sha[:10]} {c.subject[:60]} ({len(cases)}/{target})", file=sys.stderr)
    return cases


def build_clean(pool: dict[str, list[Commit]], blobs: dict[str, BlobReader], target: int,
                exclude: set[str]) -> list[dict]:
    order = {}
    for repo, commits in pool.items():
        cs = [c for c in commits if c.sha not in exclude]
        random.Random(f"{SEED}:clean:{repo}").shuffle(cs)
        order[repo] = cs
    cases = []
    for c in interleave(order):
        if len(cases) >= target:
            break
        files = load_files(c.repo, c.parents[0], c.sha, blobs[c.repo])
        if files is None:
            continue
        number, title, body = pr_info(c)
        case = base_case(c, "clean", number, title, body, files, c.parents[0], c.sha)
        case["id"] = f"{c.repo.split('/')[1]}-{number}-clean"
        cases.append(case)
    return cases


# --------------------------------------------------------------------------- main
def write_jsonl(path: Path, cases: list[dict]) -> str:
    text = "".join(json.dumps(c, ensure_ascii=False, sort_keys=False) + "\n" for c in cases)
    path.write_text(text, encoding="utf-8", newline="\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    import warnings

    warnings.filterwarnings("ignore", category=SyntaxWarning)  # old escape sequences in history
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="fetch clones and re-pin refs to current HEAD")
    ap.add_argument("--repos", nargs="*", default=REPOS)
    ap.add_argument("--out", type=Path, default=DATA)
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text("utf-8")) if MANIFEST.exists() and not args.refresh else {}
    pins: dict[str, str] = dict(manifest.get("repos", {}))

    pool: dict[str, list[Commit]] = {}
    all_commits: dict[str, list[Commit]] = {}
    blobs: dict[str, BlobReader] = {}
    for repo in args.repos:
        ensure_clone(repo, args.refresh)
        if repo not in pins:
            pins[repo] = git(repo_dir(repo), "rev-parse", "HEAD").decode().strip()
        commits = scan_commits(repo, pins[repo])
        commits.sort(key=lambda c: (c.timestamp, c.sha))
        all_commits[repo] = commits
        pool[repo] = [c for c in commits if pr_info(c) and not SKIP_TITLE.match(pr_info(c)[1])
                      and reviewable(c)]
        blobs[repo] = BlobReader(repo_dir(repo))
        print(f"{repo}: {len(commits)} first-parent commits, {len(pool[repo])} reviewable PRs",
              file=sys.stderr)

    try:
        injected, used = build_injected(pool, blobs, TARGETS["injected"])
        reverted = build_reverted(all_commits, blobs, TARGETS["reverted"], exclude=used, pins=pins)
        used |= {c["source"]["commit"] for c in reverted}
        clean = build_clean(pool, blobs, TARGETS["clean"], exclude=used)
    finally:
        for b in blobs.values():
            b.close()

    hashes = {
        "injected": write_jsonl(args.out / "injected.jsonl", injected),
        "reverted": write_jsonl(args.out / "reverted.jsonl", reverted),
        "clean": write_jsonl(args.out / "clean.jsonl", clean),
        "v1": write_jsonl(args.out / "v1.jsonl", injected + reverted + clean),
    }
    counts = {
        "injected": len(injected), "reverted": len(reverted), "clean": len(clean),
        "injected_by_kind": {k: sum(1 for c in injected if c["expected"]["bug_kind"] == k) for k in mut.KINDS},
    }
    (args.out / "manifest.json").write_text(json.dumps({
        "seed": SEED, "targets": TARGETS, "repos": pins, "counts": counts, "sha256": hashes,
    }, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
