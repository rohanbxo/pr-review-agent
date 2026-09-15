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


def test_whole_file_range():
    assert R.whole_file_range({"content": "x\n" * 50}) == {"start": 1, "end": 50}
    assert R.whole_file_range({"content": ""}) == {"start": 1, "end": 1}
    assert R.whole_file_range({"patch": "@@ -3,2 +3,3 @@\n x\n@@ -40 +41,2 @@\n q"}) == {"start": 1, "end": 42}


def test_baseline_flags_every_changed_file_as_medium_whole_file():
    fs = R.baseline_findings(CASE)
    assert [(f["file"], f["lines"], f["severity"]) for f in fs] == [
        ("src/a.py", {"start": 1, "end": 50}, "medium"),
        ("src/new.py", {"start": 1, "end": 1}, "medium"),
    ]


def test_expected_changed_line_span_uses_the_expected_files_patch():
    assert R.expected_changed_line_span(CASE) is None  # clean
    bug = {**CASE, "expected": {"bug_kind": "flipped_comparison", "file": "src/a.py",
                                "lines": {"start": 4, "end": 4}}}
    assert R.expected_changed_line_span(bug) == 39  # added lines 4 and 42


def test_baseline_detects_everything_and_localises_nothing():
    """The property the baseline exists to demonstrate."""
    from eval import metrics as M

    bug = {**CASE, "id": "demo-bug", "split": "injected",
           "expected": {"bug_kind": "flipped_comparison", "file": "src/a.py", "lines": {"start": 42, "end": 42}}}
    results = asyncio.run(R.run_all([bug, CASE], mode="baseline", concurrency=1, timeout_s=1,
                                    max_tool_rounds=None))
    rep = M.compute_report_metrics(results)
    assert rep["detection"]["overall"]["detection_rate"] == 1.0
    assert rep["localisation"]["overall"]["localisation_rate_of_detected"] == 0.0
    assert rep["false_positive_rate"]["rate"] == 1.0


def test_scoring_flags_are_validated_and_recorded(tmp_path):
    import json

    ds = tmp_path / "d.jsonl"
    ds.write_text(json.dumps(CASE) + "\n", encoding="utf-8")
    assert R.main(["--baseline", "--dataset", str(ds), "--report", str(tmp_path / "bad.json"),
                   "--narrow-span-fraction", "0"]) == 2
    out = tmp_path / "b.json"
    assert R.main(["--baseline", "--dataset", str(ds), "--report", str(out), "--tolerance", "2",
                   "--narrow-max-lines", "12", "--narrow-span-fraction", "0.5"]) == 0
    scoring = json.loads(out.read_text(encoding="utf-8"))["meta"]["scoring"]
    assert (scoring["tolerance_lines"], scoring["narrow_max_lines"], scoring["narrow_span_fraction"]) == (2, 12, 0.5)


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
