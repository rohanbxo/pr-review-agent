import ast
import random
import textwrap

import pytest

from eval.mutations import (
    KINDS,
    added_lines_from_patch,
    apply_candidate,
    count_changes,
    find_candidates,
    mutate,
    pyflakes_messages,
    split_lines,
    unified_patch,
)


def _src(s: str) -> str:
    return textwrap.dedent(s).lstrip("\n")


def _run(source: str, fn: str, *args):
    ns: dict = {}
    exec(compile(source, "<test>", "exec"), ns)
    try:
        return ("ok", ns[fn](*args))
    except Exception as exc:  # the mutant may crash instead of returning a wrong value
        return ("raised", type(exc).__name__)


def _check_common(original: str, m, all_lines=True):
    ast.parse(m.source)
    assert not (pyflakes_messages(m.source) - pyflakes_messages(original))
    assert ast.dump(ast.parse(m.source)) != ast.dump(ast.parse(original))
    n = len(split_lines(m.source))
    assert 1 <= m.lines["start"] <= m.lines["end"] <= n


# ----------------------------------------------------------------------- flipped_comparison
FLIP_SRC = _src("""
    def clamp(value, limit):
        # keep the comment
        if value < limit:
            return value
        return limit
""")


@pytest.mark.parametrize("op,flipped", [("<", ">="), (">=", "<"), (">", "<="), ("<=", ">"),
                                        ("==", "!="), ("!=", "==")])
def test_flipped_comparison_all_ops(op, flipped):
    src = FLIP_SRC.replace("value < limit", f"value {op} limit")
    m = mutate("flipped_comparison", src)
    assert m is not None
    _check_common(src, m)
    assert f"value {flipped} limit" in m.source
    assert m.lines == {"start": 3, "end": 3}
    assert split_lines(m.source)[2].strip() == f"if value {flipped} limit:"
    # everything else is byte-identical
    assert m.source.replace(f"value {flipped} limit", f"value {op} limit") == src


def test_flipped_comparison_changes_behaviour():
    m = mutate("flipped_comparison", FLIP_SRC)
    assert _run(FLIP_SRC, "clamp", 1, 5) != _run(m.source, "clamp", 1, 5)


def test_flipped_comparison_ignores_chained_and_shift():
    src = _src("""
        def f(a, b, c):
            return a < b < c, a << b
    """)
    assert find_candidates("flipped_comparison", src) == []


# ----------------------------------------------------------------------- removed_none_guard
GUARD_SRC = _src("""
    def length(items):
        if items is None:
            return 0
        return len(items)
""")


def test_removed_none_guard_statement():
    m = mutate("removed_none_guard", GUARD_SRC)
    assert m is not None
    _check_common(GUARD_SRC, m)
    assert "is None" not in m.source
    assert _run(GUARD_SRC, "length", None) == ("ok", 0)
    assert _run(m.source, "length", None) == ("raised", "TypeError")
    # localised to where the guard used to be (line 1 = def, line 2 = next statement)
    assert m.lines == {"start": 1, "end": 2}
    assert split_lines(m.source)[1].strip() == "return len(items)"


def test_removed_none_guard_raise():
    src = _src("""
        def name_of(obj):
            if obj.name is None:
                raise ValueError("unnamed")
            return obj.name.upper()
    """)
    m = mutate("removed_none_guard", src)
    assert m is not None and "raise" not in m.source
    _check_common(src, m)


def test_removed_none_guard_clause():
    src = _src("""
        def positive(x):
            return x is not None and x > 0
    """)
    m = mutate("removed_none_guard", src)
    assert m is not None
    _check_common(src, m)
    assert "return x > 0" in m.source
    assert m.lines == {"start": 2, "end": 2}
    assert _run(src, "positive", None) == ("ok", False)
    assert _run(m.source, "positive", None) == ("raised", "TypeError")


def test_removed_none_guard_skips_sole_statement_and_unused_names():
    # removing the only statement of a body would not parse
    only = _src("""
        def f(x):
            if x is None:
                return 0
    """)
    assert mutate("removed_none_guard", only) is None
    # removing the guard would leave `msg` unused -> pyflakes regression -> rejected
    unused = _src("""
        def f(x):
            msg = "missing"
            if x is None:
                raise ValueError(msg)
            return x
    """)
    assert mutate("removed_none_guard", unused) is None


# ----------------------------------------------------------------------- off_by_one_range_len
RANGE_SRC = _src("""
    def total(xs):
        acc = 0
        for i in range(len(xs)):
            acc += xs[i]
        return acc
""")


def test_off_by_one_both_variants():
    cands = find_candidates("off_by_one_range_len", RANGE_SRC)
    outs = {apply_candidate(RANGE_SRC, c).source.splitlines()[2].strip() for c in cands}
    assert outs == {"for i in range(len(xs) - 1):", "for i in range(1, len(xs)):"}
    for c in cands:
        m = apply_candidate(RANGE_SRC, c)
        _check_common(RANGE_SRC, m)
        assert m.lines == {"start": 3, "end": 3}
        assert _run(m.source, "total", [1, 2, 3]) != _run(RANGE_SRC, "total", [1, 2, 3])


def test_off_by_one_requires_exact_shape():
    src = _src("""
        def f(xs):
            return list(range(len(xs) - 1)), list(range(0, len(xs)))
    """)
    assert find_candidates("off_by_one_range_len", src) == []


# ----------------------------------------------------------------------- transposed_args
SWAP_SRC = _src("""
    def ratio(numerator, denominator):
        return divide(numerator, denominator)


    def divide(a, b):
        return a / b
""")


def test_transposed_args():
    m = mutate("transposed_args", SWAP_SRC)
    assert m is not None
    _check_common(SWAP_SRC, m)
    assert "divide(denominator, numerator)" in m.source
    assert m.lines == {"start": 2, "end": 2}
    assert _run(SWAP_SRC, "ratio", 1, 4) == ("ok", 0.25)
    assert _run(m.source, "ratio", 1, 4) == ("ok", 4.0)


def test_transposed_args_skips_noop_and_non_names():
    src = _src("""
        def f(a, b, obj):
            return max(a, b), min(a, b), g(a, 1), g(a, a), g(obj.x, obj.y)
        def g(x, y):
            return x - y
    """)
    cands = find_candidates("transposed_args", src)
    assert [c.description.split("`")[1:4:2] for c in cands] == [["obj.x", "obj.y"]]


# ----------------------------------------------------------------------- localisation
def test_only_pr_added_lines_are_mutated():
    base = _src("""
        def f(a, b):
            if a < b:
                return a
            return b
    """)
    head = base.replace("    return b\n", "    if a == b:\n        return 0\n    return b\n")
    patch = unified_patch(base, head)
    added = added_lines_from_patch(patch)
    assert added == {4, 5}
    # line 2 (`a < b`) is pre-existing: must not be chosen even though it is a site
    m = mutate("flipped_comparison", head, added)
    assert m is not None
    assert m.lines == {"start": 4, "end": 4}
    assert "if a != b:" in m.source and "if a < b:" in m.source
    # the regenerated patch against base still contains the (mutated) change only
    new_patch = unified_patch(base, m.source)
    assert "+    if a != b:" in new_patch
    assert count_changes(new_patch) == (2, 0)
    assert mutate("flipped_comparison", head, {99}) is None


def test_added_lines_from_git_style_patch():
    patch = "@@ -1,3 +1,4 @@\n a\n-b\n+B\n+C\n c\n@@ -10 +11,2 @@\n x\n+y\n\\ No newline at end of file"
    assert added_lines_from_patch(patch) == {2, 3, 12}


def test_multibyte_columns_are_respected():
    src = 'def f(x):\n    s = "héllo wörld"; return len(s) < x\n'
    m = mutate("flipped_comparison", src)
    assert m is not None and m.source == src.replace("< x", ">= x")


def test_mutate_is_deterministic():
    src = FLIP_SRC + "\n\ndef g(a, b):\n    return a == b or a > b\n"
    runs = {mutate("flipped_comparison", src, rng=random.Random(7)).source for _ in range(3)}
    assert len(runs) == 1


def test_every_kind_has_a_finder():
    for k in KINDS:
        find_candidates(k, "x = 1\n")
    with pytest.raises(ValueError):
        find_candidates("nope", "x = 1\n")
