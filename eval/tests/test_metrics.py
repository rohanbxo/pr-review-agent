import pytest

from eval import metrics as M


def F(file="a.py", start=10, end=None, severity="medium"):
    return {"file": file, "lines": {"start": start, "end": end or start}, "severity": severity}


def R(split, findings=(), expected=None, error=None, duration=None, usage=None, id="x"):
    return {"id": id, "split": split, "findings": list(findings), "expected": expected,
            "error": error, "duration_s": duration, "usage": usage}


def E(file="a.py", start=20, end=None, kind="flipped_comparison"):
    return {"bug_kind": kind, "file": file, "lines": {"start": start, "end": end or start}}


def test_ranges_overlap_with_tolerance():
    assert M.ranges_overlap(10, 12, 12, 14, 0)
    assert not M.ranges_overlap(10, 12, 13, 14, 0)
    assert M.ranges_overlap(10, 12, 17, 20, 5)
    assert not M.ranges_overlap(10, 12, 18, 20, 5)
    assert M.ranges_overlap(18, 20, 10, 12, 6)  # symmetric
    with pytest.raises(ValueError):
        M.ranges_overlap(1, 1, 1, 1, -1)


def test_default_tolerance_is_five():
    assert M.DEFAULT_TOLERANCE == 5
    assert M.localise(F(start=15), E(start=20)) == "line"
    assert M.localise(F(start=14), E(start=20)) == "file"
    assert M.localise(F(start=14), E(start=20), tolerance=6) == "line"


def test_localise_file_matching():
    assert M.localise(F(file="./src/a.py"), E(file="src/a.py", start=10)) == "line"
    assert M.localise(F(file="b.py"), E(start=10)) is None
    assert M.localise(F(), None) is None


def test_severity_threshold():
    assert not M.is_medium_plus(F(severity="low"))
    assert all(M.is_medium_plus(F(severity=s)) for s in ("medium", "high", "critical", "HIGH"))
    assert not M.is_medium_plus({"severity": "bogus"})


def test_false_positive_rate():
    results = [
        R("clean"),
        R("clean", [F(severity="low")]),
        R("clean", [F(severity="high"), F()]),
        R("clean", error="boom"),
        R("injected", [F()], expected=E()),  # not a clean case: ignored
    ]
    fp = M.false_positive_rate(results)
    assert fp["cases"] == 4
    assert fp["false_positives"] == 2  # the high finding + the errored case
    assert fp["rate"] == 0.5
    assert fp["errored_counted_as_fp"] == 1
    assert fp["mean_medium_plus_findings_per_clean_case"] == 0.5


def test_detection_by_split_and_kind():
    results = [
        R("injected", [F(start=21)], expected=E(start=20)),                           # hit
        R("injected", [F(start=21, severity="low")], expected=E(start=20)),           # low: miss
        R("injected", [F(file="b.py", start=20)], expected=E(start=20, kind="transposed_args")),  # wrong file
        R("reverted", [F(start=100)], expected=E(start=20, kind="reverted_fix")),     # file only: miss
        R("reverted", [F(start=18, end=19)], expected=E(start=20, end=25, kind="reverted_fix"), error="x"),
        R("clean", [F()]),
    ]
    d = M.detection(results)
    assert d["overall"] == {"rate": 0.2, "detected": 1, "cases": 5, "errored": 1}
    assert list(d["by_split"]) == ["injected", "reverted"]
    assert d["by_split"]["injected"]["detected"] == 1 and d["by_split"]["injected"]["cases"] == 3
    assert d["by_split"]["reverted"]["rate"] == 0.0
    assert d["by_bug_kind"]["flipped_comparison"] == {"rate": 0.5, "detected": 1, "cases": 2, "errored": 0}
    assert d["by_bug_kind"]["transposed_args"]["detected"] == 0
    assert d["by_bug_kind"]["reverted_fix"]["errored"] == 1


def test_localisation_file_only_share():
    results = [
        R("injected", [F(start=20)], expected=E(start=20)),                 # line
        R("injected", [F(start=90), F(start=22)], expected=E(start=20)),    # line (any finding)
        R("injected", [F(start=90)], expected=E(start=20)),                 # file only
        R("injected", [F(file="z.py")], expected=E(start=20)),              # none
    ]
    loc = M.localisation(results)
    assert loc["tolerance_lines"] == 5
    assert (loc["line_match"], loc["file_only"], loc["file_match"]) == (2, 1, 3)
    assert loc["file_only_share"] == round(1 / 3, 4)
    assert loc["line_match_rate"] == 0.5
    assert M.localisation(results, tolerance=100)["file_only"] == 0


def test_cost():
    results = [R("clean", duration=float(i), usage={"input_tokens": 10 * i, "output_tokens": i,
                                                    "total_tokens": 11 * i}) for i in range(1, 21)]
    results.append(R("clean", error="x"))
    c = M.cost(results)
    assert c["cases_timed"] == 20
    assert c["mean_seconds"] == 10.5
    assert c["p95_seconds"] == 19.0
    assert c["mean_total_tokens"] == 115.5
    assert M.cost([])["mean_seconds"] is None


def test_percentile_nearest_rank():
    assert M.percentile([5], 95) == 5
    assert M.percentile([1, 2, 3, 4], 50) == 2
    assert M.percentile([], 95) is None


def test_report_groups_in_mandated_order():
    rep = M.compute_report_metrics([R("clean"), R("injected", [F()], expected=E(start=10))])
    assert list(rep)[:4] == ["false_positive_rate", "detection", "localisation", "cost"]
    assert rep["errors"]["count"] == 0
