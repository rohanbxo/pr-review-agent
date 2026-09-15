import asyncio

from eval import run_eval as R


CASE = {
    "id": "demo-1-clean", "split": "clean", "repo": "octo/demo", "pr_number": 1, "title": "t", "body": "",
    "base_sha": "a" * 40, "head_sha": "b" * 40, "expected": None,
    "files": [
        {"filename": "src/a.py", "status": "modified", "additions": 1, "deletions": 0,
         "patch": "@@ -3,2 +3,3 @@\n x\n+y\n z\n@@ -40 +41,2 @@\n q\n+r", "content": "x\n" * 50},
        {"filename": "src/new.py", "status": "added", "additions": 1, "deletions": 0,
         "patch": "@@ -0,0 +1 @@\n+hello", "content": "hello"},
        {"filename": "src/gone.py", "status": "removed", "additions": 0, "deletions": 1,
         "patch": "@@ -1 +0,0 @@\n-bye"},
    ],
}


def test_first_hunk_range():
    assert R.first_hunk_range("@@ -3,2 +3,3 @@\n x") == {"start": 3, "end": 5}
    assert R.first_hunk_range("@@ -0,0 +1 @@\n+hello") == {"start": 1, "end": 1}
    assert R.first_hunk_range("@@ -5,2 +4,0 @@\n-a\n-b") == {"start": 4, "end": 4}
    assert R.first_hunk_range(None) == {"start": 1, "end": 1}


def test_baseline_flags_every_changed_file_as_medium():
    fs = R.baseline_findings(CASE)
    assert [(f["file"], f["lines"], f["severity"]) for f in fs] == [
        ("src/a.py", {"start": 3, "end": 5}, "medium"),
        ("src/new.py", {"start": 1, "end": 1}, "medium"),
    ]


def test_select_cases_round_robin():
    cases = [{"id": f"{s}{i}", "split": s} for s in ("injected", "reverted", "clean") for i in range(5)]
    assert [c["id"] for c in R.select_cases(cases, None, 4)] == ["injected0", "reverted0", "clean0", "injected1"]
    assert [c["split"] for c in R.select_cases(cases, ["clean"], None)] == ["clean"] * 5


def test_fake_llm_cannot_write_protected_reports(tmp_path):
    assert R.main(["--llm", "fake", "--report", str(tmp_path / "v1.json")]) == 2
    assert R.main(["--llm", "fake", "--report", str(tmp_path / "results.json")]) == 2
    assert not list(tmp_path.iterdir())


def test_agent_mode_fails_fast_without_key(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    try:
        assert R.main(["--report", str(tmp_path / "v1.json")]) == 2
    finally:
        get_settings.cache_clear()
    assert not list(tmp_path.iterdir())


def test_fake_llm_goes_through_real_graph_client_and_transport():
    res = asyncio.run(R.run_agent_case(CASE, R.make_fake_llm, timeout_s=60, max_tool_rounds=None))
    assert res["error"] is None, res.get("traceback")
    assert res["findings"] == []
    assert res["blocked_calls"] == []
    assert res["github_calls"] >= 3  # PR + files (fetch_context) + tool calls
