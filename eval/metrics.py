"""Scoring. Pure functions over per-case results -- no I/O, no agent imports.

A *case result* is a dict::

    {"id", "split", "expected": {"bug_kind", "file", "lines": {"start", "end"}} | None,
     "findings": [{"file", "lines": {"start", "end"}, "severity", ...}],
     "duration_s": float | None, "usage": {"input_tokens", "output_tokens", "total_tokens"} | None,
     "error": str | None}

The four metric groups are always produced together, in this order (SPEC phase 6):

1. ``false_positive_rate`` on ``clean`` -- share of clean cases with any finding at medium+.
2. ``detection`` -- by split and by bug kind. A detection is a medium+ finding localised to the
   expected bug (file match AND line overlap within ``tolerance``).
3. ``localisation`` -- line-level vs file-only matches.
4. ``cost`` -- mean / p95 seconds, mean tokens.

Errored cases count as failures in their split: on bug splits they are misses, on ``clean`` they
count as false positives (a review that did not complete is not a clean pass). They are also
reported separately under ``errors``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

DEFAULT_TOLERANCE = 5
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
CLEAN_SPLITS = frozenset({"clean"})
BUG_SPLITS = ("injected", "reverted", "injection")


def is_medium_plus(finding: dict) -> bool:
    return SEVERITY_RANK.get(str(finding.get("severity", "")).lower(), -1) >= SEVERITY_RANK["medium"]


def normalise_path(path: str | None) -> str:
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int, tolerance: int = DEFAULT_TOLERANCE) -> bool:
    """True if [a_start, a_end] and [b_start, b_end] overlap once widened by ``tolerance``."""
    if tolerance < 0:
        raise ValueError("tolerance must be >= 0")
    a_start, a_end = min(a_start, a_end), max(a_start, a_end)
    b_start, b_end = min(b_start, b_end), max(b_start, b_end)
    return a_start <= b_end + tolerance and b_start <= a_end + tolerance


def _range(obj: dict | None) -> tuple[int, int] | None:
    if not obj:
        return None
    try:
        s, e = int(obj["start"]), int(obj.get("end", obj["start"]))
    except (KeyError, TypeError, ValueError):
        return None
    return s, e


def localise(finding: dict, expected: dict | None, tolerance: int = DEFAULT_TOLERANCE) -> str | None:
    """``"line"`` (file + range overlap), ``"file"`` (file only), or ``None``."""
    if not expected or normalise_path(finding.get("file")) != normalise_path(expected.get("file")):
        return None
    f, e = _range(finding.get("lines")), _range(expected.get("lines"))
    if f and e and ranges_overlap(*f, *e, tolerance=tolerance):
        return "line"
    return "file"


def case_outcome(result: dict, tolerance: int = DEFAULT_TOLERANCE) -> dict:
    """Per-case scoring: {medium_plus, flagged, match: "line"|"file"|None, detected, errored}."""
    errored = bool(result.get("error"))
    findings = [] if errored else [f for f in (result.get("findings") or []) if is_medium_plus(f)]
    match = None
    for f in findings:
        m = localise(f, result.get("expected"), tolerance)
        if m == "line":
            match = "line"
            break
        if m == "file":
            match = "file"
    return {
        "medium_plus": len(findings),
        "flagged": errored or bool(findings),
        "match": match,
        "detected": (not errored) and match == "line",
        "errored": errored,
    }


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def percentile(values: Iterable[float], pct: float) -> float | None:
    """Nearest-rank percentile (p in (0, 100])."""
    vs = sorted(values)
    if not vs:
        return None
    rank = max(1, math.ceil(pct / 100 * len(vs)))
    return vs[rank - 1]


def false_positive_rate(results: list[dict], tolerance: int = DEFAULT_TOLERANCE) -> dict:
    clean = [r for r in results if r.get("split") in CLEAN_SPLITS]
    outs = [case_outcome(r, tolerance) for r in clean]
    fp = sum(o["flagged"] for o in outs)
    return {
        "rate": _rate(fp, len(clean)),
        "false_positives": fp,
        "cases": len(clean),
        "errored_counted_as_fp": sum(o["errored"] for o in outs),
        "mean_medium_plus_findings_per_clean_case": (
            round(sum(o["medium_plus"] for o in outs) / len(outs), 3) if outs else None),
    }


def _detection_group(rs: list[dict], tolerance: int) -> dict:
    outs = [case_outcome(r, tolerance) for r in rs]
    hit = sum(o["detected"] for o in outs)
    return {"rate": _rate(hit, len(rs)), "detected": hit, "cases": len(rs),
            "errored": sum(o["errored"] for o in outs)}


def _bug_results(results: list[dict]) -> list[dict]:
    return [r for r in results if r.get("split") not in CLEAN_SPLITS and r.get("expected")]


def detection(results: list[dict], tolerance: int = DEFAULT_TOLERANCE) -> dict:
    bugs = _bug_results(results)
    splits = sorted({r["split"] for r in bugs},
                    key=lambda s: (BUG_SPLITS.index(s) if s in BUG_SPLITS else len(BUG_SPLITS), s))
    kinds = sorted({r["expected"].get("bug_kind") or "unknown" for r in bugs})
    return {
        "overall": _detection_group(bugs, tolerance),
        "by_split": {s: _detection_group([r for r in bugs if r["split"] == s], tolerance) for s in splits},
        "by_bug_kind": {k: _detection_group([r for r in bugs if (r["expected"].get("bug_kind") or "unknown") == k],
                                            tolerance) for k in kinds},
    }


def localisation(results: list[dict], tolerance: int = DEFAULT_TOLERANCE) -> dict:
    """Line-level vs file-only matching over bug cases (medium+ findings only).

    ``file_only_share`` = of the cases where some medium+ finding named the right file, the share
    where none of them landed within ``tolerance`` lines -- i.e. how much of a file-level
    "detection" would be a whole-file shrug."""
    bugs = _bug_results(results)
    outs = [case_outcome(r, tolerance) for r in bugs]
    line = sum(o["match"] == "line" for o in outs)
    file_only = sum(o["match"] == "file" for o in outs)
    file_level = line + file_only
    return {
        "tolerance_lines": tolerance,
        "cases": len(bugs),
        "file_match": file_level,
        "file_match_rate": _rate(file_level, len(bugs)),
        "line_match": line,
        "line_match_rate": _rate(line, len(bugs)),
        "file_only": file_only,
        "file_only_share": _rate(file_only, file_level),
    }


def cost(results: list[dict]) -> dict:
    durations = [float(r["duration_s"]) for r in results if r.get("duration_s") is not None]
    tokens = [int((r.get("usage") or {}).get("total_tokens") or 0) for r in results if r.get("usage")]
    ins = [int((r.get("usage") or {}).get("input_tokens") or 0) for r in results if r.get("usage")]
    outs = [int((r.get("usage") or {}).get("output_tokens") or 0) for r in results if r.get("usage")]
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
    p95 = percentile(durations, 95)
    return {
        "cases_timed": len(durations),
        "mean_seconds": mean(durations),
        "p95_seconds": round(p95, 3) if p95 is not None else None,
        "mean_total_tokens": mean(tokens),
        "mean_input_tokens": mean(ins),
        "mean_output_tokens": mean(outs),
    }


def errors(results: list[dict]) -> dict:
    errs = [r for r in results if r.get("error")]
    by_split: dict[str, int] = {}
    for r in errs:
        by_split[r.get("split", "?")] = by_split.get(r.get("split", "?"), 0) + 1
    return {"count": len(errs), "by_split": by_split,
            "cases": [{"id": r.get("id"), "split": r.get("split"), "error": r.get("error")} for r in errs]}


def compute_report_metrics(results: list[dict], tolerance: int = DEFAULT_TOLERANCE) -> dict:
    """All four groups, in the mandated order, plus errors."""
    return {
        "false_positive_rate": false_positive_rate(results, tolerance),
        "detection": detection(results, tolerance),
        "localisation": localisation(results, tolerance),
        "cost": cost(results),
        "errors": errors(results),
    }
