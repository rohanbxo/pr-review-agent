"""Per-case checkpointing: a killed run keeps what it paid for, resume finishes the job, and a
resumed report is identical to an uninterrupted one. Config drift is refused."""

import json

import pytest

from eval import checkpoint as C
from eval import run_eval as R
from eval.tests.test_run_eval import CASE, _dataset, llm_env  # noqa: F401  (fixture)


def cases(n):
    return [{**CASE, "id": f"c{i}", "pr_number": i + 1} for i in range(n)]


def read_ckpt(path):
    header, results = C.Checkpoint(path, {}).read()
    return header, [r["id"] for r in results]


def run(tmp_path, ds, name, *extra):
    out = tmp_path / f"{name}-fake.json"
    code = R.main(["--llm", "fake", "--dataset", str(ds), "--report", str(out), "--concurrency", "1", *extra])
    return code, out


def test_each_case_is_on_disk_before_the_next_one_starts(tmp_path, llm_env, monkeypatch):
    """The property that makes a kill survivable: after case k completes, case k is durable."""
    llm_env()
    ds = _dataset(tmp_path, cases(4))
    seen_on_disk = []
    real = R.run_agent_case

    async def spy(case, *a, **kw):
        header, ids = read_ckpt(tmp_path / "spy-fake.cases.jsonl")
        seen_on_disk.append(list(ids))
        return await real(case, *a, **kw)

    monkeypatch.setattr(R, "run_agent_case", spy)
    code, _ = run(tmp_path, ds, "spy")
    assert code == 0
    assert seen_on_disk == [[], ["c0"], ["c0", "c1"], ["c0", "c1", "c2"]]


def test_interrupted_run_resumes_and_matches_an_uninterrupted_run(tmp_path, llm_env, monkeypatch):
    llm_env()
    ds = _dataset(tmp_path, cases(5))

    # 1. uninterrupted reference run
    code, ref_path = run(tmp_path, ds, "ref")
    assert code == 0
    ref = json.loads(ref_path.read_text(encoding="utf-8"))

    # 2. a run that dies after 2 cases (simulating the OOM kill)
    real = R.run_agent_case
    calls = {"n": 0}

    async def die_after_two(case, *a, **kw):
        if calls["n"] >= 2:
            raise KeyboardInterrupt("killed")
        calls["n"] += 1
        return await real(case, *a, **kw)

    monkeypatch.setattr(R, "run_agent_case", die_after_two)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, ds, "crash")
    header, done = read_ckpt(tmp_path / "crash-fake.cases.jsonl")
    assert done == ["c0", "c1"]  # paid for, kept
    assert header["mode"] == "fake"

    # 3. resume: only the unfinished cases run, and the report matches the reference
    monkeypatch.setattr(R, "run_agent_case", real)
    code, out = run(tmp_path, ds, "crash", "--resume")
    assert code == 0
    resumed = json.loads(out.read_text(encoding="utf-8"))
    assert [c["id"] for c in resumed["cases"]] == [c["id"] for c in ref["cases"]]
    for key in ("false_positive_rate", "detection", "localisation", "parse_errors", "errors"):
        assert resumed[key] == ref[key]
    assert "partial_run" not in resumed["meta"]  # complete, not partial


def test_resume_refuses_a_checkpoint_from_a_different_config(tmp_path, llm_env, capsys):
    llm_env()
    ds = _dataset(tmp_path, cases(2))
    assert run(tmp_path, ds, "drift")[0] == 0
    ckpt = tmp_path / "drift-fake.cases.jsonl"

    out = tmp_path / "drift2-fake.json"
    code = R.main(["--llm", "fake", "--dataset", str(ds), "--report", str(out), "--resume",
                   "--checkpoint", str(ckpt), "--tolerance", "9"])
    assert code == 2
    err = capsys.readouterr().err
    assert "different configuration" in err and "scoring" in err
    assert not out.exists()

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = _dataset(other_dir, cases(3))  # different dataset_sha256
    code = R.main(["--llm", "fake", "--dataset", str(other), "--report", str(tmp_path / "d3-fake.json"),
                   "--resume", "--checkpoint", str(ckpt)])
    assert code == 2 and "dataset_sha256" in capsys.readouterr().err


def test_existing_checkpoint_is_not_silently_appended_to(tmp_path, llm_env, capsys):
    llm_env()
    ds = _dataset(tmp_path, cases(2))
    assert run(tmp_path, ds, "guard")[0] == 0
    code, _ = run(tmp_path, ds, "guard")  # same report path again, no --resume
    assert code == 2
    assert "already exists" in capsys.readouterr().err


def test_report_from_checkpoint_is_marked_partial(tmp_path, llm_env):
    llm_env()
    ds = _dataset(tmp_path, cases(6))
    ckpt = tmp_path / "part-fake.cases.jsonl"
    assert R.main(["--llm", "fake", "--dataset", str(ds), "--report", str(tmp_path / "part-fake.json"),
                   "--limit", "2", "--concurrency", "1"]) == 0

    out = tmp_path / "part2-fake.json"
    assert R.main(["--llm", "fake", "--dataset", str(ds), "--report", str(out), "--from-checkpoint",
                   "--checkpoint", str(ckpt)]) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["meta"]["partial_run"] == {"cases_reported": 2, "cases_selected": 6, "checkpoint": str(ckpt)}
    assert "PARTIAL RUN: 2 of 6" in rep["WARNING"]
    assert len(rep["cases"]) == 2


def test_torn_last_line_is_ignored(tmp_path, llm_env):
    """A hard kill can leave a half-written line; it must not break resume."""
    llm_env()
    ds = _dataset(tmp_path, cases(3))
    assert R.main(["--llm", "fake", "--dataset", str(ds), "--report", str(tmp_path / "torn-fake.json"),
                   "--limit", "2", "--concurrency", "1"]) == 0
    ckpt = tmp_path / "torn-fake.cases.jsonl"
    with ckpt.open("a", encoding="utf-8") as fh:
        fh.write('{"id": "c2", "split": "clean", "find')  # torn
    code, out = run(tmp_path, ds, "torn", "--resume")
    assert code == 0
    assert [c["id"] for c in json.loads(out.read_text(encoding="utf-8"))["cases"]] == ["c0", "c1", "c2"]


def test_fingerprint_covers_model_temperature_and_trimming(tmp_path):
    """Drift the eval CLI cannot produce in fake mode (no model is configured) is still refused."""
    base = dict(mode="configured", dataset_sha="abc", provider="openai_compatible",
                model="anthropic/claude-haiku-4.5", temperature=0.0, keep_tool_results="off",
                scoring={"scoring_version": 3})
    path = tmp_path / "fp.cases.jsonl"
    with C.Checkpoint(path, C.fingerprint(**base)) as ck:
        ck.open(resuming=False)
        ck.append({"id": "c0"})

    assert [r["id"] for r in C.Checkpoint(path, C.fingerprint(**base)).load_for_resume()] == ["c0"]
    for field, value in [("model", "anthropic/claude-sonnet-5"), ("temperature", 0.7),
                         ("keep_tool_results", 2), ("provider", "anthropic"), ("mode", "fake")]:
        with pytest.raises(C.CheckpointMismatch, match="different configuration"):
            C.Checkpoint(path, C.fingerprint(**{**base, field: value})).load_for_resume()


def test_header_is_required(tmp_path):
    path = tmp_path / "headerless.cases.jsonl"
    path.write_text('{"id": "c0"}\n', encoding="utf-8")
    with pytest.raises(C.CheckpointMismatch, match="no checkpoint header"):
        C.Checkpoint(path, {"mode": "fake"}).load_for_resume()


def test_resume_does_not_parse_finished_cases_in_full(tmp_path, llm_env):
    """Memory: on a resume the finished cases are stubs, not full patches and file contents."""
    llm_env()
    ds = _dataset(tmp_path, cases(4))
    assert R.main(["--llm", "fake", "--dataset", str(ds), "--report", str(tmp_path / "lean-fake.json"),
                   "--limit", "2", "--concurrency", "1"]) == 0
    done = {r["id"] for r in C.Checkpoint(tmp_path / "lean-fake.cases.jsonl", {}).read()[1]}
    loaded, _ = R.load_dataset(ds, skip_ids=done)
    by_id = {c["id"]: c for c in loaded}
    assert set(by_id) == {f"c{i}" for i in range(4)}
    for cid in done:
        assert set(by_id[cid]) == {"id", "split"}          # stub only
    for cid in set(by_id) - done:
        assert by_id[cid]["files"][0]["content"]           # still complete for the cases that run
