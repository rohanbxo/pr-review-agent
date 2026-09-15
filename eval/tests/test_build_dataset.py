from eval import build_dataset as B
from eval.mutations import added_lines_from_patch, unified_patch


BUGGY = "def f(x):\n    a = 1\n    b = 2\n    return x + 1\n\n\ndef g():\n    pass\n"
FIXED = "def f(x):\n    a = 1\n    b = 2\n    if x is None:\n        return 0\n    return x + 1\n\n\ndef g():\n    pass\n"


def test_revert_fix_onto_later_version():
    later = "import os\n\n\n" + FIXED.replace("    pass\n", "    return os.getcwd()\n")
    head = B.revert_fix_onto(BUGGY, FIXED, later)
    assert head == "import os\n\n\n" + BUGGY.replace("    pass\n", "    return os.getcwd()\n")
    # the later, unrelated change survives; the fix is gone
    assert "os.getcwd()" in head and "is None" not in head


def test_revert_fix_onto_refuses_when_fixed_code_moved():
    later = FIXED.replace("    if x is None:\n", "    if x is None or x < 0:\n")
    assert B.revert_fix_onto(BUGGY, FIXED, later) is None
    # ambiguous (block occurs twice)
    assert B.revert_fix_onto(BUGGY, FIXED, FIXED + "\n" + FIXED) is None


def test_parse_diff_and_touched_lines():
    raw = ("diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
           "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+z\n")
    files = B.parse_diff(raw)
    assert files["x.py"]["status"] == "modified" and files["n.py"]["status"] == "added"
    assert files["x.py"]["patch"].startswith("@@ -1,2 +1,2 @@")
    p = unified_patch(FIXED, BUGGY)  # pure deletion of the guard
    assert not added_lines_from_patch(p)
    assert B.touched_head_lines(p) == {3, 4}


def test_pr_info_formats():
    merge = B.Commit("o/r", "s", ["p1", "p2"], 0, "a", "Merge pull request #12 from u/b\n\nNice title\n\nBody")
    squash = B.Commit("o/r", "s", ["p1"], 0, "a", "Fix thing (#34)\n\ndetails")
    plain = B.Commit("o/r", "s", ["p1"], 0, "a", "fix thing")
    assert B.pr_info(merge) == (12, "Nice title", "Body")
    assert B.pr_info(squash) == (34, "Fix thing", "details")
    assert B.pr_info(plain) is None


def test_path_classification():
    assert B.is_test_path("tests/test_x.py") and B.is_test_path("src/pkg/conftest.py")
    assert B.is_source_py("src/flask/app.py")
    assert not B.is_source_py("docs/conf.py") and not B.is_source_py("tests/a.py")
    assert B.VENDORED.search("src/pip/_vendor/x.py")
