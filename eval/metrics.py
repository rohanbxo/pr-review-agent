"""Scoring. Pure functions over per-case results -- no I/O, no agent imports.

A *case result* is a dict::

    {"id", "split", "expected": {"bug_kind", "file", "lines": {"start", "end"}} | None,
     "expected_changed_line_span": int | None,   # required for bug cases, see changed_line_span()
     "findings": [{"file", "lines": {"start", "end"}, "severity", ...}],
     "duration_s": float | None, "usage": {"input_tokens", "output_tokens", "total_tokens"} | None,
     "error": str | None}

The four metric groups are always produced together, in this order (SPEC phase 6):

1. ``false_positive_rate`` on ``clean`` -- share of clean cases with any finding at medium+.
2. ``detection`` -- by split and by bug kind, as a share of ALL bug cases. A finding DETECTS the
   bug if it is medium+, names the expected file, and its range overlaps the bug's within
   ``tolerance_lines``.
3. ``localisation`` -- by split and by bug kind, as a share of DETECTED bugs. A detecting finding
   LOCALISES the bug if its range is also narrow (``ScoringConfig.narrow_limit``). A finding that
   spans the whole file detects everything and localises nothing.
4. ``cost`` -- mean / p95 seconds, mean tokens.

The thresholds live in ``ScoringConfig`` and are written into every report (``meta.scoring``), so
a later threshold change cannot silently make old reports incomparable.

Errored cases count as failures in their split: on bug splits they are misses, on ``clean`` they
count as false positives (a review that did not complete is not a clean pass). They are also
reported separately under ``errors``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass

DEFAULT_TOLERANCE = 5
DEFAULT_NARROW_MAX_LINES = 30
DEFAULT_NARROW_SPAN_FRACTION = 0.25
SCORING_VERSION = 2

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
CLEAN_SPLITS = frozenset({"clean"})
BUG_SPLITS = ("injected", "reverted", "injection")


@dataclass(frozen=True)
class ScoringConfig:
    tolerance_lines: int = DEFAULT_TOLERANCE
    narrow_max_lines: int = DEFAULT_NARROW_MAX_LINES
    narrow_span_fraction: float = DEFAULT_NARROW_SPAN_FRACTION

    def __post_init__(self) -> None:
        if self.tolerance_lines < 0 or self.narrow_max_lines < 1 or not 0 < self.narrow_span_fraction <= 1:
            raise ValueError(f"invalid scoring config: {self}")

    def narrow_limit(self, bug_width: int, changed_line_span: int) -> float:
        """Widest finding range (in lines) that still counts as a pin rather than a shrug.

        ``min(narrow_max_lines, narrow_span_fraction * changed_line_span)``: thirty lines on a
        small change is a shrug, on a large one a real pin. Floored at the bug's own width,
        otherwise a one-line change (limit 0.25) or a multi-line bug could never be localised."""
        return max(float(bug_width),
                   min(float(self.narrow_max_lines), self.narrow_span_fraction * changed_line_span))

    def as_dict(self) -> dict:
        return {
            "scoring_version": SCORING_VERSION,
            **asdict(self),
            "detection_rule": "medium+ AND same file AND range overlap within tolerance_lines",
            "localisation_rule": ("detection AND finding_width <= max(bug_width, min(narrow_max_lines, "
                                  "narrow_span_fraction * changed_line_span))"),
            "changed_line_span": ("max - min + 1 over the head lines the expected file's patch adds, "
                                  "or deletes next to; context lines excluded"),
        }


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


def _width(r: tuple[int, int]) -> int:
    return abs(r[1] - r[0]) + 1


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def changed_head_lines(patch: str | None) -> set[int]:
    """Head line numbers the patch adds, plus the head lines either side of pure deletions."""
    lines: set[int] = set()
    new_line = 0
    for raw in (patch or "").splitlines():
        m = _HUNK.match(raw)
        if m:
            new_line = int(m.group(1))
        elif raw.startswith("+"):
            lines.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            lines.update({max(1, new_line - 1), max(1, new_line)})
        elif not raw.startswith("\\"):
            new_line += 1
    return lines


def changed_line_span(patch: str | None) -> int:
    touched = changed_head_lines(patch)
    return max(touched) - min(touched) + 1 if touched else 0


def detects(finding: dict, expected: dict | None, cfg: ScoringConfig) -> bool:
    """Right file, and the finding's range overlaps the bug within tolerance. Severity is the
    caller's filter."""
    if not expected or normalise_path(finding.get("file")) != normalise_path(expected.get("file")):
        return False
    f, e = _range(finding.get("lines")), _range(expected.get("lines"))
    return bool(f and e and ranges_overlap(*f, *e, tolerance=cfg.tolerance_lines))


def localises(finding: dict, expected: dict | None, span: int, cfg: ScoringConfig) -> bool:
    """``detects`` AND the finding's range is narrow."""
    if not detects(finding, expected, cfg):
        return False
    f, e = _range(finding.get("lines")), _range(expected.get("lines"))
    return _width(f) <= cfg.narrow_limit(_width(e), span)


def case_outcome(result: dict, cfg: ScoringConfig | None = None) -> dict:
    """Per-case scoring: {medium_plus, flagged, detected, localised, errored}."""
    cfg = cfg or ScoringConfig()
    errored = bool(result.get("error"))
    findings = [] if errored else [f for f in (result.get("findings") or []) if is_medium_plus(f)]
    expected = result.get("expected")
    detected = localised = False
    if expected and findings:
        span = result.get("expected_changed_line_span")
        if span is None:
            raise ValueError(f"case {result.get('id')!r}: expected_changed_line_span is required to "
                             "score localisation")
        detected = any(detects(f, expected, cfg) for f in findings)
        localised = any(localises(f, expected, int(span), cfg) for f in findings)
    return {
        "medium_plus": len(findings),
        "flagged": errored or bool(findings),
        "detected": detected,
        "localised": localised,
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


def false_positive_rate(results: list[dict], cfg: ScoringConfig | None = None) -> dict:
    clean = [r for r in results if r.get("split") in CLEAN_SPLITS]
    outs = [case_outcome(r, cfg) for r in clean]
    fp = sum(o["flagged"] for o in outs)
    return {
        "rate": _rate(fp, len(clean)),
        "false_positives": fp,
        "cases": len(clean),
        "errored_counted_as_fp": sum(o["errored"] for o in outs),
        "mean_medium_plus_findings_per_clean_case": (
            round(sum(o["medium_plus"] for o in outs) / len(outs), 3) if outs else None),
    }


def _bug_results(results: list[dict]) -> list[dict]:
    return [r for r in results if r.get("split") not in CLEAN_SPLITS and r.get("expected")]


def _grouped(results: list[dict], group_fn) -> dict:
    bugs = _bug_results(results)
    splits = sorted({r["split"] for r in bugs},
                    key=lambda s: (BUG_SPLITS.index(s) if s in BUG_SPLITS else len(BUG_SPLITS), s))
    kinds = sorted({r["expected"].get("bug_kind") or "unknown" for r in bugs})
    return {
        "overall": group_fn(bugs),
        "by_split": {s: group_fn([r for r in bugs if r["split"] == s]) for s in splits},
        "by_bug_kind": {k: group_fn([r for r in bugs if (r["expected"].get("bug_kind") or "unknown") == k])
                        for k in kinds},
    }


def detection(results: list[dict], cfg: ScoringConfig | None = None) -> dict:
    """Share of ALL bug cases with a detecting finding."""
    def group(rs: list[dict]) -> dict:
        outs = [case_outcome(r, cfg) for r in rs]
        hit = sum(o["detected"] for o in outs)
        return {"detection_rate": _rate(hit, len(rs)), "detected": hit, "cases": len(rs),
                "errored": sum(o["errored"] for o in outs)}
    return _grouped(results, group)


def localisation(results: list[dict], cfg: ScoringConfig | None = None) -> dict:
    """Share of DETECTED bugs that were also localised. The denominator is ``detected``, not all
    bug cases -- hence the field name. ``localised_share_of_all_bugs`` is there for convenience."""
    def group(rs: list[dict]) -> dict:
        outs = [case_outcome(r, cfg) for r in rs]
        det = sum(o["detected"] for o in outs)
        loc = sum(o["localised"] for o in outs)
        return {"localisation_rate_of_detected": _rate(loc, det), "localised": loc, "detected": det,
                "cases": len(rs), "localised_share_of_all_bugs": _rate(loc, len(rs))}
    return _grouped(results, group)


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


def parse_errors(results: list[dict]) -> dict:
    """Structured-output health of synthesize, over every evaluated case.

    ``failed_attempts`` counts attempts that did not yield a valid ReviewResult. A case is
    ``repaired`` if it failed first and parsed on the retry, ``unrecovered`` if synthesis gave up
    (the case then also appears under ``errors``). If this is not ~0, the other numbers are noise."""
    run = len(results)
    failing = [r for r in results if int(r.get("parse_failures") or 0) > 0 or r.get("synthesis_failed")]
    unrecovered = sum(1 for r in results if r.get("synthesis_failed"))
    return {
        "cases_run": run,
        "cases_with_parse_failure": len(failing),
        "case_rate": _rate(len(failing), run),
        "repaired_cases": len(failing) - unrecovered,
        "unrecovered_cases": unrecovered,
        "failed_attempts": sum(int(r.get("parse_failures") or 0) for r in results),
        "case_ids": [r.get("id") for r in failing],
    }


def compute_report_metrics(results: list[dict], cfg: ScoringConfig | None = None) -> dict:
    """All four groups, in the mandated order, plus errors, parse errors and the scoring config used."""
    cfg = cfg or ScoringConfig()
    return {
        "false_positive_rate": false_positive_rate(results, cfg),
        "detection": detection(results, cfg),
        "localisation": localisation(results, cfg),
        "cost": cost(results),
        "errors": errors(results),
        "parse_errors": parse_errors(results),
        "scoring": cfg.as_dict(),
    }
