import asyncio
import json

import pytest

from eval import run_eval as R
from eval.run_eval import ROOT


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


@pytest.fixture
def llm_env(tmp_path, monkeypatch):
    """Clean LLM settings (no stray .env), applied per test."""
    from app.config import get_settings

    for var in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "AGENT_MODEL", "LLM_MODEL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)

    def apply(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()

    get_settings.cache_clear()
    yield apply
    get_settings.cache_clear()


@pytest.mark.parametrize("env", [
    {"AGENT_MODEL": "anthropic/claude-test"},                    # openai_compatible, no key
    {"LLM_API_KEY": "sk-test"},                                  # openai_compatible, no model
    {"LLM_PROVIDER": "anthropic"},                               # anthropic, no key
])
def test_agent_mode_fails_fast_when_llm_is_not_configured(tmp_path, llm_env, env, capsys):
    llm_env(**env)
    assert R.main(["--report", str(tmp_path / "out" / "v1.json")]) == 2
    assert not (tmp_path / "out").exists()
    assert "no report was written" in capsys.readouterr().err


def _load_openai_stub():
    import importlib.util
    import sys

    path = ROOT / "backend" / "tests" / "helpers" / "openai_stub.py"
    spec = importlib.util.spec_from_file_location("openai_stub_for_eval", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def _dataset(tmp_path, cases):
    ds = tmp_path / "ds.jsonl"
    ds.write_text("".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    return ds


def test_configured_mode_end_to_end_with_stub_openai_server(tmp_path, llm_env, monkeypatch):
    """The whole eval path with the REAL ChatOpenAI client and graph, a stub OpenRouter, no key:
    provider/model land in meta, and parse errors surface at the top of the report."""
    from app.agent import llm as llm_mod

    stub_mod = _load_openai_stub()
    good = json.dumps(stub_mod.VALID_REVIEW)
    # PR 1 parses first time; PR 2 fails once then repairs; PR 3 never parses.
    stub = stub_mod.OpenAIStub(payloads_by_pr={1: [good], 2: ['{"summary": 1}', good], 3: ["not json"]})
    monkeypatch.setattr(llm_mod, "default_http_async_client", stub.async_client)
    llm_env(LLM_API_KEY="sk-or-test", AGENT_MODEL="anthropic/claude-sonnet-test")

    bug = {**CASE, "id": "demo-bug", "split": "injected", "pr_number": 2,
           "expected": {"bug_kind": "flipped_comparison", "file": "src/a.py", "lines": {"start": 4, "end": 4}}}
    broken = {**CASE, "id": "demo-broken", "pr_number": 3}
    ds = _dataset(tmp_path, [CASE, bug, broken])
    out = tmp_path / "configured.json"
    assert R.main(["--dataset", str(ds), "--report", str(out), "--concurrency", "1"]) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))

    assert rep["meta"]["llm"] == "configured"
    assert rep["meta"]["provider"] == "openai_compatible"
    assert rep["meta"]["model"] == "anthropic/claude-sonnet-test"
    assert rep["meta"]["base_url"] == "https://openrouter.ai/api/v1"
    assert "sk-or-test" not in out.read_text(encoding="utf-8")

    assert list(rep)[:3] == ["meta", "parse_errors", "false_positive_rate"]
    pe = rep["parse_errors"]
    assert (pe["cases_run"], pe["cases_with_parse_failure"], pe["repaired_cases"], pe["unrecovered_cases"],
            pe["failed_attempts"]) == (3, 2, 1, 1, 3)
    assert sorted(pe["case_ids"]) == ["demo-broken", "demo-bug"]
    by_id = {c["id"]: c for c in rep["cases"]}
    assert by_id["demo-broken"]["synthesis_failed"] and "SynthesisError" in by_id["demo-broken"]["error"]
    assert by_id["demo-1-clean"]["usage"]["total_tokens"] > 0
    assert {r["authorization"] for r in stub.requests} == {"Bearer sk-or-test"}
    assert {r["body"]["model"] for r in stub.requests} == {"anthropic/claude-sonnet-test"}


def test_provider_and_model_overrides_are_recorded(tmp_path, llm_env, monkeypatch):
    llm_env(ANTHROPIC_API_KEY="sk-ant-test")
    captured = {}

    async def fake_run_all(cases, **kw):
        captured.update(kw)
        return [R._result_stub(c) for c in cases]

    monkeypatch.setattr(R, "run_all", fake_run_all)
    ds = _dataset(tmp_path, [CASE])
    out = tmp_path / "haiku.json"
    assert R.main(["--dataset", str(ds), "--report", str(out), "--provider", "anthropic",
                   "--model", "claude-haiku-4-5-20251001"]) == 0
    meta = json.loads(out.read_text(encoding="utf-8"))["meta"]
    assert (meta["provider"], meta["model"], meta["base_url"]) == ("anthropic", "claude-haiku-4-5-20251001", None)
    assert captured["llm_config"].provider == "anthropic"


def test_every_mode_records_provider_and_model(tmp_path):
    ds = _dataset(tmp_path, [CASE])
    for argv, provider, model in [
        (["--baseline"], "none", "baseline:whole-file"),
        (["--llm", "fake"], "fake", "fake-review (stub)"),
    ]:
        out = tmp_path / f"{provider}-fake.json"
        assert R.main(["--dataset", str(ds), "--report", str(out), *argv]) == 0
        meta = json.loads(out.read_text(encoding="utf-8"))["meta"]
        assert (meta["provider"], meta["model"]) == (provider, model)
        assert "parse_errors" in json.loads(out.read_text(encoding="utf-8"))


def test_fake_llm_goes_through_real_graph_client_and_transport():
    res = asyncio.run(R.run_agent_case(CASE, R.make_fake_llm, timeout_s=60, max_tool_rounds=None))
    assert res["error"] is None, res.get("traceback")
    assert res["findings"] == []
    assert res["blocked_calls"] == []
    assert res["github_calls"] >= 3  # PR + files (fetch_context) + tool calls
