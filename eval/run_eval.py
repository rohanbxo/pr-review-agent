"""Run the eval.

    python -m eval.run_eval --dataset eval/data/v1.jsonl --report eval/reports/v1.json
    python -m eval.run_eval --baseline                   # -> eval/reports/baseline.json
    python -m eval.run_eval --llm fake --limit 3         # offline smoke -> eval/reports/smoke-fake.json

Agent mode runs the REAL graph (``app.agent.graph.review_pull_request``) through the REAL
read-only client (allowlist included) over ``app.agent.fixtures.mock_transport_for_case`` -- no
GitHub traffic. ``--baseline`` uses no LLM: one ``medium`` finding per changed file spanning the
whole file -- it detects every bug and localises none, which is the bar the agent must clear.
See eval/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from eval import metrics as M  # noqa: E402

DEFAULT_DATASET = ROOT / "eval" / "data" / "v1.jsonl"
REPORTS = ROOT / "eval" / "reports"
PROTECTED_REPORTS = {"v1.json", "baseline.json"}


# --------------------------------------------------------------------------- dataset
def load_dataset(path: Path) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    cases = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    return cases, hashlib.sha256(raw).hexdigest()


def select_cases(cases: list[dict], splits: list[str] | None, limit: int | None) -> list[dict]:
    if splits:
        cases = [c for c in cases if c["split"] in splits]
    if limit is None or limit >= len(cases):
        return cases
    # round-robin across splits so a small --limit still touches every split
    by_split: dict[str, list[dict]] = {}
    for c in cases:
        by_split.setdefault(c["split"], []).append(c)
    out: list[dict] = []
    i = 0
    while len(out) < limit:
        for s in by_split:
            if i < len(by_split[s]) and len(out) < limit:
                out.append(by_split[s][i])
        i += 1
    return out


# --------------------------------------------------------------------------- baseline
def whole_file_range(file: dict) -> dict:
    """Line 1 through the last line of the head file (or of the last hunk if content is absent)."""
    if file.get("content") is not None:
        return {"start": 1, "end": max(1, len(file["content"].splitlines()))}
    ends = [int(m.group(1)) + int(m.group(2) if m.group(2) is not None else 1) - 1
            for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", file.get("patch") or "", flags=re.M)]
    return {"start": 1, "end": max([1, *ends])}


def baseline_findings(case: dict) -> list[dict]:
    return [
        {"file": f["filename"], "lines": whole_file_range(f), "severity": "medium",
         "title": "Changed file", "detail": "Baseline: every changed file is flagged, whole file.",
         "suggestion": None}
        for f in case.get("files") or [] if f.get("status") != "removed"
    ]


def expected_changed_line_span(case: dict) -> int | None:
    expected = case.get("expected")
    if not expected:
        return None
    f = next((f for f in case.get("files") or []
              if M.normalise_path(f["filename"]) == M.normalise_path(expected.get("file"))), None)
    return M.changed_line_span(f.get("patch")) if f else 0


# --------------------------------------------------------------------------- fake LLM
def make_fake_llm(case: dict):
    """An offline stand-in with the same surface the graph uses (``bind_tools``,
    ``with_structured_output(..., include_raw=True)``). Round 1 calls two tools (so the tools
    node, the client allowlist and the mock transport are exercised); round 2 stops; synthesize
    returns an EMPTY review. It measures plumbing, never quality."""
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.runnables import RunnableLambda

    from app.agent.schema import ReviewResult

    first = next((f["filename"] for f in case.get("files") or [] if f.get("content") is not None), None)
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    class FakeReviewLLM(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "fake-review"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            tool_round = any(isinstance(m, ToolMessage) for m in messages)
            calls = [] if tool_round else [{"name": "list_changed_files", "args": {}, "id": "fake-1"}] + (
                [{"name": "read_file", "args": {"path": first}, "id": "fake-2"}] if first else [])
            msg = AIMessage(content="" if calls else "done", tool_calls=calls, usage_metadata=usage)
            return ChatResult(generations=[ChatGeneration(message=msg)])

        def with_structured_output(self, schema, *, include_raw=False, **kwargs):
            def run(_messages):
                parsed = ReviewResult(summary="fake LLM: no review performed", risk="low",
                                      findings=[], files_reviewed=[])
                raw = AIMessage(content=parsed.model_dump_json(), usage_metadata=usage)
                return {"raw": raw, "parsed": parsed, "parsing_error": None} if include_raw else parsed
            return RunnableLambda(run)

    return FakeReviewLLM()


# --------------------------------------------------------------------------- runners
def _result_stub(case: dict) -> dict:
    return {"id": case["id"], "split": case["split"], "repo": case["repo"], "pr_number": case["pr_number"],
            "expected": case.get("expected"), "expected_changed_line_span": expected_changed_line_span(case),
            "findings": [], "summary": None, "risk": None,
            "duration_s": None, "usage": None, "github_calls": 0, "blocked_calls": [],
            "dropped_findings": 0, "error": None}


async def run_agent_case(case: dict, llm_factory, timeout_s: float, max_tool_rounds: int | None) -> dict:
    from app.agent.fixtures import mock_transport_for_case
    from app.agent.github_client import ReadOnlyGitHubClient
    from app.agent.graph import review_pull_request

    res = _result_stub(case)
    t0 = time.perf_counter()
    client = ReadOnlyGitHubClient(token=None, transport=mock_transport_for_case(case))
    try:
        outcome = await asyncio.wait_for(
            review_pull_request(repo=case["repo"], pr_number=int(case["pr_number"]), client=client,
                                llm=llm_factory(case), metadata={"eval_case": case["id"]},
                                max_tool_rounds=max_tool_rounds),
            timeout=timeout_s,
        )
        result = outcome.result.model_dump(mode="json")
        res.update(findings=result["findings"], summary=result["summary"], risk=result["risk"],
                   duration_s=round(outcome.duration_s, 3), usage=dict(outcome.usage),
                   dropped_findings=len(outcome.dropped_findings))
    except Exception as exc:  # an errored case is a failure in its split, reported separately
        res["error"] = f"{type(exc).__name__}: {exc}"[:2000]
        res["traceback"] = traceback.format_exc(limit=5)[-4000:]
        res["duration_s"] = round(time.perf_counter() - t0, 3)
    finally:
        res["github_calls"] = len(client.calls)
        res["blocked_calls"] = [c.as_dict() for c in client.calls if c.blocked]
        await client.aclose()
    return res


async def run_all(cases: list[dict], *, mode: str, concurrency: int, timeout_s: float,
                  max_tool_rounds: int | None) -> list[dict]:
    if mode == "baseline":
        out = []
        for case in cases:
            t0 = time.perf_counter()
            res = _result_stub(case)
            res["findings"] = baseline_findings(case)
            res["duration_s"] = round(time.perf_counter() - t0, 6)
            res["usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            out.append(res)
        return out

    if mode == "fake":
        llm_factory = make_fake_llm
    else:
        from app.agent.llm import get_llm

        shared = get_llm()
        llm_factory = lambda _case: shared  # noqa: E731

    sem = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async def one(case):
        nonlocal done
        async with sem:
            r = await run_agent_case(case, llm_factory, timeout_s, max_tool_rounds)
        done += 1
        status = "ERROR " + r["error"][:80] if r["error"] else f"{len(r['findings'])} findings"
        print(f"  [{done}/{len(cases)}] {case['id']}: {status} ({r['duration_s']}s)", file=sys.stderr)
        return r

    return list(await asyncio.gather(*(one(c) for c in cases)))


# --------------------------------------------------------------------------- report
def project_git_sha() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                              check=True, text=True).stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def build_report(results: list[dict], *, mode: str, model: str | None, dataset: Path, dataset_sha: str,
                 all_cases: list[dict], scoring: M.ScoringConfig, args: argparse.Namespace) -> dict:
    metrics = M.compute_report_metrics(results, scoring)
    meta = {
        "llm": mode,  # "anthropic" | "fake" | "baseline"
        "model": model,
        "dataset": str(dataset),
        "dataset_sha256": dataset_sha,
        "dataset_counts": dict(Counter(c["split"] for c in all_cases)),
        "evaluated_counts": dict(Counter(r["split"] for r in results)),
        "evaluated_by_bug_kind": dict(Counter((r.get("expected") or {}).get("bug_kind") or "none"
                                              for r in results)),
        # Every threshold that shaped the numbers. Compare two reports only if these match.
        "scoring": metrics["scoring"],
        "limit": args.limit,
        "splits": args.splits,
        "concurrency": args.concurrency,
        "git_sha": project_git_sha(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": platform.python_version(),
    }
    report: dict = {}
    if mode == "fake":
        report["WARNING"] = ("llm=fake: offline plumbing smoke test with a stub model that returns an "
                             "empty review. These numbers say NOTHING about review quality.")
    report["meta"] = meta
    for key in ("false_positive_rate", "detection", "localisation", "cost", "errors"):  # SPEC order
        report[key] = metrics[key]
    report["cases"] = results
    return report


def _pct(x) -> str:
    return "  n/a" if x is None else f"{100 * x:5.1f}%"


def summary_table(report: dict) -> str:
    m, fp, det, loc, cost = (report["meta"], report["false_positive_rate"], report["detection"],
                             report["localisation"], report["cost"])
    sc = m["scoring"]
    lines = [
        f"PR review eval  llm={m['llm']}  model={m['model']}",
        f"scoring v{sc['scoring_version']}: tolerance={sc['tolerance_lines']} lines, narrow <= "
        f"max(bug width, min({sc['narrow_max_lines']}, {sc['narrow_span_fraction']} x changed-line span))",
        f"dataset {m['dataset']}  sha256={m['dataset_sha256'][:12]}  evaluated={m['evaluated_counts']}",
        "",
        "1. False-positive rate on clean (any medium+ finding)",
        f"   {_pct(fp['rate'])}   ({fp['false_positives']}/{fp['cases']}; errored counted as FP: "
        f"{fp['errored_counted_as_fp']}; mean medium+ findings/clean case: "
        f"{fp['mean_medium_plus_findings_per_clean_case']})",
        "",
        "2. Detection rate, share of ALL bugs (medium+, right file, overlaps bug within tolerance)",
        f"   {'group':28s} {'rate':>6s} {'hit':>5s} {'cases':>6s} {'errored':>8s}",
    ]
    for label, group in [("", {"overall": det["overall"]}), ("split: ", det["by_split"]),
                         ("kind: ", det["by_bug_kind"])]:
        for name, g in group.items():
            lines.append(f"   {label + name:28s} {_pct(g['detection_rate'])} {g['detected']:5d} "
                         f"{g['cases']:6d} {g['errored']:8d}")
    lines += ["", "3. Localisation rate, share of DETECTED bugs (detection AND narrow range)",
              f"   {'group':28s} {'rate':>6s} {'loc':>5s} {'det':>6s}"]
    for label, group in [("", {"overall": loc["overall"]}), ("split: ", loc["by_split"]),
                         ("kind: ", loc["by_bug_kind"])]:
        for name, g in group.items():
            lines.append(f"   {label + name:28s} {_pct(g['localisation_rate_of_detected'])} "
                         f"{g['localised']:5d} {g['detected']:6d}")
    lines += [
        "",
        "4. Cost",
        f"   mean {cost['mean_seconds']}s  p95 {cost['p95_seconds']}s  mean tokens {cost['mean_total_tokens']}",
        "",
        f"errors: {report['errors']['count']} {report['errors']['by_split']}",
    ]
    if "WARNING" in report:
        lines.insert(0, "!!! " + report["WARNING"])
    return "\n".join(lines)


# --------------------------------------------------------------------------- main
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--report", type=Path, default=None,
                    help="default: eval/reports/{v1,baseline,smoke-fake}.json by mode")
    ap.add_argument("--baseline", action="store_true", help="no LLM: flag every changed file as medium")
    ap.add_argument("--llm", choices=["anthropic", "fake"], default="anthropic",
                    help="'fake' is an offline plumbing smoke test; its report may not be v1/baseline")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--splits", nargs="*", default=None, choices=["injected", "reverted", "clean", "injection"])
    ap.add_argument("--tolerance", type=int, default=M.DEFAULT_TOLERANCE,
                    help="detection: max line gap between finding and bug ranges")
    ap.add_argument("--narrow-max-lines", type=int, default=M.DEFAULT_NARROW_MAX_LINES,
                    help="localisation: absolute cap on a finding's width")
    ap.add_argument("--narrow-span-fraction", type=float, default=M.DEFAULT_NARROW_SPAN_FRACTION,
                    help="localisation: cap as a fraction of the file's changed-line span")
    ap.add_argument("--case-timeout", type=float, default=600.0)
    ap.add_argument("--max-tool-rounds", type=int, default=None)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        scoring = M.ScoringConfig(tolerance_lines=args.tolerance, narrow_max_lines=args.narrow_max_lines,
                                  narrow_span_fraction=args.narrow_span_fraction)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    mode = "baseline" if args.baseline else args.llm
    if args.report is None:
        args.report = REPORTS / {"baseline": "baseline.json", "fake": "smoke-fake.json",
                                 "anthropic": "v1.json"}[mode]

    if mode == "fake" and (args.report.name in PROTECTED_REPORTS or "fake" not in args.report.name):
        print(f"error: --llm fake must not write {args.report}; use a report name containing 'fake' "
              f"(e.g. eval/reports/smoke-fake.json).", file=sys.stderr)
        return 2
    if mode == "baseline" and args.report.name == "v1.json":
        print("error: refusing to write the baseline over eval/reports/v1.json.", file=sys.stderr)
        return 2

    model: str | None = None
    if mode == "anthropic":
        from app.config import get_settings

        settings = get_settings()
        if not (os.environ.get("ANTHROPIC_API_KEY") or settings.anthropic_api_key):
            print("error: ANTHROPIC_API_KEY is not set (env or backend .env). The agent eval needs a real "
                  "model; no report was written.\n  Offline plumbing check: python -m eval.run_eval "
                  "--llm fake --limit 3\n  No-LLM baseline:        python -m eval.run_eval --baseline",
                  file=sys.stderr)
            return 2
        model = settings.llm_model
    elif mode == "fake":
        model = "fake-review (stub)"

    if not args.dataset.exists():
        print(f"error: dataset {args.dataset} not found (python -m eval.build_dataset)", file=sys.stderr)
        return 2
    all_cases, sha = load_dataset(args.dataset)
    cases = select_cases(all_cases, args.splits, args.limit)
    print(f"running {len(cases)} cases in mode={mode}", file=sys.stderr)

    results = asyncio.run(run_all(cases, mode=mode, concurrency=args.concurrency,
                                  timeout_s=args.case_timeout, max_tool_rounds=args.max_tool_rounds))
    report = build_report(results, mode=mode, model=model, dataset=args.dataset, dataset_sha=sha,
                          all_cases=all_cases, scoring=scoring, args=args)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(summary_table(report))
    print(f"\nreport written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
