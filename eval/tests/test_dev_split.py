import hashlib
import json
import random

import pytest

from eval import dev_split as D
from eval import metrics as M

KINDS = D.INJECTED_KINDS


def _cases():
    cases = []
    for i in range(40):
        cases.append({"id": f"inj-{i}", "split": "injected",
                      "expected": {"bug_kind": KINDS[i % 4], "file": "a.py", "lines": {"start": 1, "end": 1}}})
    cases += [{"id": f"rev-{i}", "split": "reverted", "expected": {"bug_kind": "reverted_fix"}} for i in range(25)]
    cases += [{"id": f"clean-{i}", "split": "clean", "expected": None} for i in range(150)]
    return cases


def test_stratified_counts_and_kind_balance():
    dev = D.select_dev(_cases())
    counts = {s: sum(c["split"] == s for c in dev) for s in ("injected", "reverted", "clean")}
    assert counts == {"injected": 15, "reverted": 10, "clean": 35}
    kinds = sorted(sum(c["expected"]["bug_kind"] == k for c in dev if c["split"] == "injected") for k in KINDS)
    assert kinds == [3, 4, 4, 4]


def test_selection_depends_only_on_ids_not_order():
    cases = _cases()
    shuffled = cases[:]
    random.Random(1).shuffle(shuffled)
    assert [c["id"] for c in D.select_dev(cases)] == [c["id"] for c in D.select_dev(shuffled)]


def test_new_cases_only_enter_by_their_own_hash():
    cases = _cases()
    before = {c["id"] for c in D.select_dev(cases)}
    for n in range(20):
        new = {"id": f"new-clean-{n}", "split": "clean", "expected": None}
        after = {c["id"] for c in D.select_dev(cases + [new])}
        assert after == before or (after - before == {new["id"]} and len(before - after) == 1)


def test_too_few_cases_is_an_error():
    with pytest.raises(ValueError):
        D.select_dev([c for c in _cases() if c["split"] != "reverted"])


def test_dev_file_has_its_own_dataset_hash(tmp_path):
    """A dev run and a full run carry different dataset_sha256, so the comparison rule refuses them."""
    cases = _cases()
    full = "".join(json.dumps(c) + "\n" for c in cases)
    dev = D.to_jsonl(D.select_dev(cases))
    assert hashlib.sha256(full.encode()).hexdigest() != hashlib.sha256(dev.encode()).hexdigest()


def test_committed_manifest_pins_dev_hash_when_dataset_present():
    manifest = json.loads((D.DATA / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["dev"] == D.DEV_TARGETS
    assert len(manifest["sha256"]["dev"]) == 64


def test_real_dev_split_loads_and_scores_if_built():
    """Runs against eval/data/dev.jsonl when it has been generated (make eval-dev)."""
    path = D.DATA / "dev.jsonl"
    if not path.exists():
        pytest.skip("eval/data/dev.jsonl not generated")
    from eval import run_eval as R

    cases, sha = R.load_dataset(path)
    assert sha == json.loads((D.DATA / "manifest.json").read_text(encoding="utf-8"))["sha256"]["dev"]
    assert {s: sum(c["split"] == s for c in cases) for s in ("injected", "reverted", "clean")} == D.DEV_TARGETS
    results = [R._result_stub(c) | {"findings": R.baseline_findings(c)} for c in cases]
    rep = M.compute_report_metrics(results)
    assert rep["detection"]["overall"]["detection_rate"] == 1.0
    assert rep["false_positive_rate"]["cases"] == 35
