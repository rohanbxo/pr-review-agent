"""Linter-surviving synthetic bug injection.

Mutations are *located* with the ``ast`` module and *applied* to the source text (byte-exact
splices), so everything the mutation does not touch -- comments, formatting, quoting -- is
preserved and the regenerated diff is as small as a human edit would be.

Every applied mutation is verified:

* the mutated source still ``ast.parse``-s;
* ``pyflakes`` reports no *new* messages (a file that already had e.g. an unused import is not
  rejected for it, but a mutation that introduces an unused variable is);
* the AST actually changed (``ast.dump`` differs), i.e. the mutation is not a no-op.

Only lines the PR added or modified (``+`` lines of the unified patch, in head-file numbering)
are eligible, so the injected bug sits inside the change under review.

Kinds (the names are part of the dataset contract):

``flipped_comparison``   ``<``<->``>=``, ``>``<->``<=``, ``==``<->``!=``
``removed_none_guard``   delete ``if x is None: return/raise ...`` or drop an ``x is (not) None``
                         clause from a boolean condition
``off_by_one_range_len`` ``range(len(x))`` -> ``range(len(x) - 1)`` or ``range(1, len(x))``
``transposed_args``      swap two adjacent positional args that are plain names/attributes
"""

from __future__ import annotations

import ast
import difflib
import random
import re
from collections import Counter
from dataclasses import dataclass, field

KINDS: tuple[str, ...] = (
    "flipped_comparison",
    "removed_none_guard",
    "off_by_one_range_len",
    "transposed_args",
)

_FLIP = {"<": ">=", ">=": "<", ">": "<=", "<=": ">", "==": "!=", "!=": "=="}
_FLIP_AST = (ast.Lt, ast.GtE, ast.Gt, ast.LtE, ast.Eq, ast.NotEq)
_OP_RE = re.compile(r"<=|>=|==|!=|<(?!<)|>(?!>)")

# Builtins where swapping two positional arguments cannot change the result.
_COMMUTATIVE_CALLS = frozenset({"max", "min", "print", "set", "frozenset"})
_COMMUTATIVE_ATTRS = frozenset({"operator.eq", "operator.ne", "operator.add", "operator.mul",
                                "math.gcd", "math.lcm", "math.hypot", "math.isclose"})


# --------------------------------------------------------------------------- text helpers
def split_lines(source: str) -> list[str]:
    """Split on ``\\n`` keeping line endings (matches ``ast`` line numbering for LF/CRLF)."""
    return re.findall(r"[^\n]*\n|[^\n]+$", source)


class _Positions:
    """Map ``ast`` (1-based line, utf-8 byte column) positions to ``str`` indices."""

    def __init__(self, source: str) -> None:
        self.lines = split_lines(source)
        self.starts: list[int] = []
        acc = 0
        for line in self.lines:
            self.starts.append(acc)
            acc += len(line)

    def index(self, lineno: int, col: int) -> int:
        line = self.lines[lineno - 1]
        return self.starts[lineno - 1] + len(line.encode("utf-8")[:col].decode("utf-8", "replace"))

    def node_span(self, node: ast.AST) -> tuple[int, int]:
        return (self.index(node.lineno, node.col_offset),
                self.index(node.end_lineno, node.end_col_offset))


def pyflakes_messages(source: str, filename: str = "<mutant>") -> Counter:
    """Line-independent multiset of pyflakes messages (``None`` values mean a syntax error)."""
    from pyflakes import checker

    tree = ast.parse(source, filename=filename)
    w = checker.Checker(tree, filename=filename)
    return Counter((type(m).__name__, tuple(str(a) for a in m.message_args)) for m in w.messages)


def added_lines_from_patch(patch: str) -> set[int]:
    """Head-file line numbers of ``+`` lines in a unified diff (``@@`` hunks only)."""
    added: set[int] = set()
    new_line = 0
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if not m:
                continue
            new_line = int(m.group(1))
        elif raw.startswith("+"):
            added.add(new_line)
            new_line += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:
            new_line += 1
    return added


def unified_patch(old: str, new: str, context: int = 3) -> str:
    """GitHub-style per-file patch (hunks only, no ``---``/``+++`` headers)."""
    old_lines, new_lines = split_lines(old), split_lines(new)
    out: list[str] = []
    for i, line in enumerate(difflib.unified_diff(old_lines, new_lines, n=context, lineterm="\n")):
        if i < 2:  # --- / +++ headers
            continue
        if not line.endswith("\n"):
            line += "\n\\ No newline at end of file\n"
        out.append(line)
    return "".join(out).rstrip("\n")


def count_changes(patch: str) -> tuple[int, int]:
    adds = dels = 0
    for raw in patch.splitlines():
        if raw.startswith("+"):
            adds += 1
        elif raw.startswith("-"):
            dels += 1
    return adds, dels


# --------------------------------------------------------------------------- candidates
@dataclass
class Candidate:
    kind: str
    start: int  # str index into the original source
    end: int
    replacement: str
    focus_lines: tuple[int, ...]  # original line numbers that must all be PR-added lines
    description: str
    deletes_lines: tuple[int, int] | None = None  # (first, last) whole lines removed


@dataclass
class Mutation:
    kind: str
    source: str  # mutated source
    lines: dict  # {"start": int, "end": int} in the mutated (head) file
    description: str
    original_lines: tuple[int, ...] = field(default_factory=tuple)


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _none_compare(node: ast.AST) -> bool:
    return (isinstance(node, ast.Compare) and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.Is, ast.IsNot)) and _is_none(node.comparators[0])
            and _dotted(node.left) is not None)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _find_flipped_comparison(tree, pos: _Positions, source: str) -> list[Candidate]:
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], _FLIP_AST)):
            continue
        gap_start = pos.index(node.left.end_lineno, node.left.end_col_offset)
        gap_end = pos.index(node.comparators[0].lineno, node.comparators[0].col_offset)
        gap = source[gap_start:gap_end]
        if "#" in gap:
            continue
        ops = list(_OP_RE.finditer(gap))
        if len(ops) != 1:
            continue
        op = ops[0].group(0)
        op_line = source.count("\n", 0, gap_start + ops[0].start()) + 1
        out.append(Candidate(
            kind="flipped_comparison",
            start=gap_start + ops[0].start(), end=gap_start + ops[0].end(),
            replacement=_FLIP[op], focus_lines=(op_line,),
            description=f"flipped comparison `{op}` -> `{_FLIP[op]}` on line {op_line}",
        ))
    return out


def _find_removed_none_guard(tree, pos: _Positions, source: str) -> list[Candidate]:
    out = []
    parents = _parents(tree)
    for node in ast.walk(tree):
        # (a) a whole `if x is None: return/raise ...` guard statement
        if (isinstance(node, ast.If) and not node.orelse and _none_compare(node.test)
                and isinstance(node.test.ops[0], ast.Is)
                and len(node.body) == 1 and isinstance(node.body[0], (ast.Return, ast.Raise))):
            parent = parents.get(node)
            body = None
            for fname in ("body", "orelse", "finalbody"):
                seq = getattr(parent, fname, None)
                if isinstance(seq, list) and node in seq:
                    body = seq
            if body is None or len(body) < 2:
                continue
            first, last = node.lineno, node.end_lineno
            lead = pos.lines[first - 1].encode("utf-8")[: node.col_offset]
            tail = pos.lines[last - 1].encode("utf-8")[node.end_col_offset:]
            if lead.strip() or (tail.strip() and not tail.strip().startswith(b"#")):
                continue  # shares a line with other code (e.g. `a = 1; if x is None: ...`)
            start = pos.starts[first - 1]
            end = pos.starts[last - 1] + len(pos.lines[last - 1])
            name = _dotted(node.test.left)
            out.append(Candidate(
                kind="removed_none_guard", start=start, end=end, replacement="",
                focus_lines=tuple(range(first, last + 1)),
                description=f"removed `if {name} is None:` guard (lines {first}-{last})",
                deletes_lines=(first, last),
            ))
        # (b) drop an `x is (not) None` clause from `a and b` / `a or b`
        if isinstance(node, ast.BoolOp) and len(node.values) >= 2:
            if node.lineno != node.end_lineno:
                continue
            for i, value in enumerate(node.values):
                if not _none_compare(value):
                    continue
                rest = [v for j, v in enumerate(node.values) if j != i]
                if any(isinstance(v, (ast.BoolOp, ast.IfExp, ast.Lambda, ast.NamedExpr))
                       for v in rest):
                    continue
                if _dotted(value.left) not in {_dotted(n) for r in rest for n in ast.walk(r)}:
                    continue  # the clause must guard something that is used afterwards
                start, end = pos.node_span(node)
                joiner = " and " if isinstance(node.op, ast.And) else " or "
                segs = [source[slice(*pos.node_span(v))] for v in rest]
                clause = source[slice(*pos.node_span(value))]
                out.append(Candidate(
                    kind="removed_none_guard", start=start, end=end,
                    replacement=joiner.join(segs), focus_lines=(node.lineno,),
                    description=f"dropped `{clause}` clause from condition on line {node.lineno}",
                ))
    return out


def _find_off_by_one(tree, pos: _Positions, source: str) -> list[Candidate]:
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "range" and len(node.args) == 1 and not node.keywords):
            continue
        arg = node.args[0]
        if not (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                and arg.func.id == "len" and len(arg.args) == 1 and not arg.keywords):
            continue
        start, end = pos.node_span(arg)
        text = source[start:end]
        for variant, repl in (("minus_one", f"{text} - 1"), ("start_one", f"1, {text}")):
            out.append(Candidate(
                kind="off_by_one_range_len", start=start, end=end, replacement=repl,
                focus_lines=tuple(range(arg.lineno, arg.end_lineno + 1)),
                description=f"`range({text})` -> `range({repl})` on line {arg.lineno} ({variant})",
            ))
    return out


def _find_transposed_args(tree, pos: _Positions, source: str) -> list[Candidate]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        if any(isinstance(a, ast.Starred) for a in node.args):
            continue
        fname = _dotted(node.func)
        if fname in _COMMUTATIVE_CALLS or fname in _COMMUTATIVE_ATTRS:
            continue
        for i in range(len(node.args) - 1):
            a, b = node.args[i], node.args[i + 1]
            da, db = _dotted(a), _dotted(b)
            if da is None or db is None or da == db:
                continue
            a_start, a_end = pos.node_span(a)
            b_start, b_end = pos.node_span(b)
            between = source[a_end:b_start]
            if between.strip() != ",":
                continue  # comments / parens between the arguments
            out.append(Candidate(
                kind="transposed_args", start=a_start, end=b_end,
                replacement=f"{source[b_start:b_end]}{between}{source[a_start:a_end]}",
                focus_lines=tuple(sorted({a.lineno, b.end_lineno})),
                description=f"swapped arguments `{da}` and `{db}` in call to `{fname or '<expr>'}` "
                            f"on line {a.lineno}",
            ))
    return out


_FINDERS = {
    "flipped_comparison": _find_flipped_comparison,
    "removed_none_guard": _find_removed_none_guard,
    "off_by_one_range_len": _find_off_by_one,
    "transposed_args": _find_transposed_args,
}


def find_candidates(kind: str, source: str, allowed_lines: set[int] | None = None) -> list[Candidate]:
    """All syntactic sites for ``kind`` whose focus lines are all in ``allowed_lines``."""
    if kind not in _FINDERS:
        raise ValueError(f"unknown mutation kind: {kind}")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    pos = _Positions(source)
    cands = _FINDERS[kind](tree, pos, source)
    if allowed_lines is not None:
        cands = [c for c in cands if all(ln in allowed_lines for ln in c.focus_lines)]
    cands.sort(key=lambda c: (c.start, c.end, c.replacement))
    return cands


def apply_candidate(source: str, cand: Candidate) -> Mutation | None:
    """Apply one candidate and verify it. Returns ``None`` if verification fails."""
    new = source[: cand.start] + cand.replacement + source[cand.end:]
    try:
        new_tree = ast.parse(new)
        old_tree = ast.parse(source)
    except SyntaxError:
        return None
    if ast.dump(new_tree) == ast.dump(old_tree):
        return None
    if pyflakes_messages(new) - pyflakes_messages(source):
        return None  # mutation introduced a lint-visible problem
    n_new = len(split_lines(new))
    if cand.deletes_lines is not None:
        first, _ = cand.deletes_lines
        start = max(1, first - 1)
        end = max(start, min(first, n_new))
    else:
        start = source.count("\n", 0, cand.start) + 1
        end = start + cand.replacement.count("\n")
    return Mutation(kind=cand.kind, source=new, lines={"start": start, "end": end},
                    description=cand.description, original_lines=cand.focus_lines)


def mutate(kind: str, source: str, allowed_lines: set[int] | None = None,
           rng: random.Random | None = None) -> Mutation | None:
    """Pick a verified mutation of ``kind`` (seeded choice among valid sites), or ``None``."""
    rng = rng or random.Random(0)
    cands = find_candidates(kind, source, allowed_lines)
    rng.shuffle(cands)
    for cand in cands:
        result = apply_candidate(source, cand)
        if result is not None:
            return result
    return None
