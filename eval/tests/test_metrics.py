import pytest

from eval import metrics as M


def F(file="a.py", start=10, end=None, severity="medium"):
    return {"file": file, "lines": {"start": start, "end": end or start}, "severity": severity}


def R(split, findings=(), expected=None, error=None, duration=None, usage=None, id="x", span=100):
    return {"id": id, "split": split, "findings": list(findings), "expected": expected,
            "expected_changed_line_span": span if expected else None,
            "error": error, "duration_s": duration, "usage": usage}


def E(file="a.py", start=20, end=None, kind="flipped_comparison"):
    return {"bug_kind": kind, "file": file, "lines": {"start": start, "end": end or start}}


CFG = M.ScoringConfig()


def test_ranges_overlap_with_tolerance():
    assert M.ranges_overlap(10, 12, 12, 14, 0)
    assert not M.ranges_overlap(10, 12, 13, 14, 0)
    assert M.ranges_overlap(10, 12, 17, 20, 5)
    assert not M.ranges_overlap(10, 12, 18, 20, 5)
    assert M.ranges_overlap(18, 20, 10, 12, 6)  # symmetric
    with pytest.raises(ValueError):
        M.ranges_overlap(1, 1, 1, 1, -1)


def test_defaults():
    assert (CFG.tolerance_lines, CFG.narrow_max_lines, CFG.narrow_span_fraction) == (5, 30, 0.25)
    assert M.detects(F(start=15), E(start=20), CFG)
    assert not M.detects(F(start=14), E(start=20), CFG)
    assert M.detects(F(start=14), E(start=20), M.ScoringConfig(tolerance_lines=6))


def test_detects_file_matching():
    assert M.detects(F(file="./src/a.py"), E(file="src/a.py", start=10), CFG)
    assert not M.detects(F(file="b.py"), E(start=10), CFG)
    assert not M.detects(F(), None, CFG)


def test_severity_threshold():
    assert not M.is_medium_plus(F(severity="low"))
    assert all(M.is_medium_plus(F(severity=s)) for s in ("medium", "high", "critical", "HIGH"))
    assert not M.is_medium_plus({"severity": "bogus"})


@pytest.mark.parametrize("bug_width,span,limit", [
    (1, 3000, 30.0),    # large change: the 30-line cap binds
    (1, 40, 10.0),      # 25% of a 40-line change
    (1, 1, 3.0),        # one-line change: the 3-line floor, not 0.25
    (1, 6, 3.0),        # v2 gave 1.5 here, so a 2-line pin on the bug scored as a shrug
    (12, 20, 12.0),     # multi-line bug wider than 25% of span: an exact pin still counts
    (1, 0, 3.0),
])
def test_narrow_limit(bug_width, span, limit):
    assert CFG.narrow_limit(bug_width, span) == limit


def test_two_line_pin_on_a_six_line_change_localises():
    """The requests-2115 case from the first dev run: correct localisation must not score zero."""
    out = M.case_outcome(R("injected", [F(start=443, end=444)], expected=E(start=443), span=6))
    assert out["detected"] and out["localised"]
    v2 = M.ScoringConfig(narrow_min_lines=1)
    assert not M.case_outcome(R("injected", [F(start=443, end=444)], expected=E(start=443), span=6), v2)["localised"]


def test_scoring_config_validates_and_serialises():
    for bad in ({"tolerance_lines": -1}, {"narrow_max_lines": 0}, {"narrow_span_fraction": 0},
                {"narrow_span_fraction": 1.5}, {"narrow_min_lines": 0}, {"narrow_min_lines": 31}):
        with pytest.raises(ValueError):
            M.ScoringConfig(**bad)
    d = M.ScoringConfig(narrow_max_lines=20).as_dict()
    assert d["narrow_max_lines"] == 20 and d["tolerance_lines"] == 5 and d["scoring_version"] == 3
    assert d["narrow_min_lines"] == 3
    assert "narrow_min_lines" in d["localisation_rule"] and "detection_rule" in d


def test_whole_file_finding_detects_but_does_not_localise():
    out = M.case_outcome(R("injected", [F(start=1, end=400)], expected=E(start=120), span=60))
    assert out["detected"] and not out["localised"]


def test_narrow_finding_localises_and_limit_scales_with_span():
    e = E(start=120)
    eight = F(start=118, end=125)                                                         # width 8
    assert M.case_outcome(R("injected", [eight], expected=e, span=40))["localised"]       # limit 10
    assert not M.case_outcome(R("injected", [eight], expected=e, span=20))["localised"]   # limit 5
    wide = F(start=100, end=135)                                   # width 36 > 30 cap, even on a huge change
    assert not M.case_outcome(R("injected", [wide], expected=e, span=3000))["localised"]


def test_localisation_requires_the_same_finding_to_detect():
    narrow_elsewhere = F(start=300, end=301)   # narrow, but does not overlap the bug
    whole = F(start=1, end=400)                # overlaps, but not narrow
    out = M.case_outcome(R("injected", [narrow_elsewhere, whole], expected=E(start=120), span=100))
    assert out["detected"] and not out["localised"]


def test_missing_span_is_an_error_not_a_guess():
    r = R("injected", [F(start=20)], expected=E(start=20))
    r["expected_changed_line_span"] = None
    with pytest.raises(ValueError):
        M.case_outcome(r)


def test_changed_line_span():
    patch = "@@ -3,2 +3,3 @@\n x\n+y\n z\n@@ -40 +41,2 @@\n q\n+r"
    assert M.changed_head_lines(patch) == {4, 42}
    assert M.changed_line_span(patch) == 39
    assert M.changed_head_lines("@@ -5,3 +5,1 @@\n a\n-b\n-c") == {5, 6}  # deletion: both neighbours
    assert M.changed_line_span(None) == 0
    assert M.changed_head_lines("@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b") == {1}


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
        R("reverted", [F(start=100)], expected=E(start=20, kind="reverted_fix")),     # right file, far: miss
        R("reverted", [F(start=18, end=19)], expected=E(start=20, end=25, kind="reverted_fix"), error="x"),
        R("clean", [F()]),
    ]
    d = M.detection(results)
    assert d["overall"] == {"detection_rate": 0.2, "detected": 1, "cases": 5, "errored": 1}
    assert list(d["by_split"]) == ["injected", "reverted"]
    assert d["by_split"]["injected"]["detected"] == 1 and d["by_split"]["injected"]["cases"] == 3
    assert d["by_split"]["reverted"]["detection_rate"] == 0.0
    assert d["by_bug_kind"]["flipped_comparison"] == {"detection_rate": 0.5, "detected": 1, "cases": 2,
                                                      "errored": 0}
    assert d["by_bug_kind"]["transposed_args"]["detected"] == 0
    assert d["by_bug_kind"]["reverted_fix"]["errored"] == 1


def test_localisation_is_a_share_of_detected_not_of_all_bugs():
    results = [
        R("injected", [F(start=20)], expected=E(start=20)),                 # detected + localised
        R("injected", [F(start=1, end=500)], expected=E(start=20)),         # detected only (shrug)
        R("injected", [F(file="z.py")], expected=E(start=20)),              # missed
        R("injected", [], expected=E(start=20)),                            # missed
        R("reverted", [F(start=1, end=500)], expected=E(start=20, kind="reverted_fix")),
    ]
    loc = M.localisation(results)
    inj = loc["by_split"]["injected"]
    assert (inj["localised"], inj["detected"], inj["cases"]) == (1, 2, 4)
    assert inj["localisation_rate_of_detected"] == 0.5    # 1 of 2 detected, not 1 of 4
    assert inj["localised_share_of_all_bugs"] == 0.25
    assert loc["by_split"]["reverted"]["localisation_rate_of_detected"] == 0.0
    nothing_detected = M.localisation([R("injected", [], expected=E())])
    assert nothing_detected["overall"]["localisation_rate_of_detected"] is None


def test_thresholds_change_the_localisation_verdict():
    shrug = [R("injected", [F(start=1, end=90)], expected=E(start=20), span=100)]
    assert M.localisation(shrug)["overall"]["localised"] == 0
    loose = M.ScoringConfig(narrow_max_lines=1000, narrow_span_fraction=1.0)
    assert M.localisation(shrug, loose)["overall"]["localised"] == 1


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


def test_report_groups_in_mandated_order_and_records_scoring():
    cfg = M.ScoringConfig(tolerance_lines=3, narrow_max_lines=12, narrow_span_fraction=0.5)
    rep = M.compute_report_metrics([R("clean"), R("injected", [F()], expected=E(start=10))], cfg)
    assert list(rep)[:4] == ["false_positive_rate", "detection", "localisation", "cost"]
    assert rep["errors"]["count"] == 0
    assert (rep["scoring"]["tolerance_lines"], rep["scoring"]["narrow_max_lines"],
            rep["scoring"]["narrow_span_fraction"]) == (3, 12, 0.5)


def test_cost_breaks_down_cache_and_provider_cost():
    u = lambda i, cr, cw, c: {"input_tokens": i, "output_tokens": 10, "total_tokens": i + 10,  # noqa: E731
                              "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw, "cost_usd": c}
    results = [R("clean", duration=1.0, usage=u(1000, 800, 100, 0.01)),
               R("clean", duration=2.0, usage=u(3000, 0, 2000, 0.03))]
    c = M.cost(results)
    assert (c["mean_cache_read_input_tokens"], c["mean_cache_creation_input_tokens"]) == (400, 1050)
    assert c["cache_read_share_of_input"] == 0.2
    assert (c["total_cost_usd"], c["mean_cost_usd"]) == (0.04, 0.02)
    legacy = M.cost([R("clean", duration=1.0, usage={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6})])
    assert legacy["mean_cache_read_input_tokens"] == 0 and legacy["total_cost_usd"] == 0.0


def test_infrastructure_failures_are_excluded_from_the_fp_rate():
    """A timeout or dropped connection is not the agent flagging a clean PR."""
    results = [
        R("clean"),                                        # quiet
        R("clean", [F(severity="high")]),                  # a real flag
        R("clean", error="TimeoutError: "),                # infra: excluded
        R("clean", error="APIConnectionError: boom"),      # infra: excluded
        R("clean", error="SynthesisError: bad json"),      # the agent failed: counts
    ]
    results[3]["infra_error"] = True
    fp = M.false_positive_rate(results)
    assert (fp["cases"], fp["false_positives"], fp["rate"]) == (3, 2, round(2 / 3, 4))
    assert fp["infrastructure_failures_excluded"] == 2
    assert fp["clean_cases_run"] == 5
    assert fp["errored_counted_as_fp"] == 1  # the SynthesisError one
    assert M.is_infrastructure_failure({"error": "TimeoutError: "})
    assert not M.is_infrastructure_failure({"error": "SynthesisError: x"})
    assert not M.is_infrastructure_failure({"error": None})
