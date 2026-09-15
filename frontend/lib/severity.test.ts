import { describe, expect, it } from "vitest";

import { extractCalls } from "./calls";
import {
  formatLineRange,
  groupFindingsByFile,
  severityLabel,
  severityRuleClass,
} from "./severity";
import type { AgentStepOut, Finding } from "./types";

describe("severity → style", () => {
  it.each(["low", "medium", "high", "critical"] as const)("%s renders as a left rule", (s) => {
    const cls = severityRuleClass(s);
    expect(cls).toContain("border-l-4");
    expect(cls).toContain(`border-l-severity-${s}`);
  });

  it("never produces a filled chip (no background or text hue)", () => {
    for (const s of ["low", "medium", "high", "critical"]) {
      expect(severityRuleClass(s)).not.toMatch(/\bbg-|\btext-|rounded-full/);
    }
  });

  it("always has a text label", () => {
    expect(severityLabel("critical")).toBe("CRITICAL");
    expect(severityLabel("medium")).toBe("MEDIUM");
  });

  it("falls back to the lowest rule for unknown values", () => {
    expect(severityRuleClass("bogus")).toBe(severityRuleClass("low"));
  });
});

describe("findings grouping", () => {
  const f = (file: string, severity: Finding["severity"], start: number): Finding => ({
    file,
    severity,
    lines: { start, end: start },
    title: "t",
    detail: "d",
    suggestion: null,
  });

  it("groups by file and sorts by severity then line", () => {
    const groups = groupFindingsByFile([
      f("a.py", "low", 1),
      f("b.py", "high", 5),
      f("a.py", "critical", 30),
      f("a.py", "critical", 10),
    ]);
    expect(groups.map(([file]) => file)).toEqual(["a.py", "b.py"]);
    expect(groups[0][1].map((x) => x.lines.start)).toEqual([10, 30, 1]);
  });

  it("formats line ranges GitHub-style", () => {
    expect(formatLineRange({ start: 12, end: 12 })).toBe("L12");
    expect(formatLineRange({ start: 12, end: 18 })).toBe("L12–L18");
  });
});

describe("extractCalls", () => {
  const step = (output: unknown): AgentStepOut => ({
    seq: 9,
    kind: "github_calls",
    name: "github_calls",
    input: null,
    output,
    latency_ms: 0,
    created_at: "2026-01-01T00:00:00Z",
  });
  const call = { method: "POST", path: "/repos/a/b/pulls", status: null, duration_ms: 0, blocked: true, error: "ReadOnlyViolation" };

  it("accepts a bare list or {calls}", () => {
    expect(extractCalls([step([call])])?.[0].blocked).toBe(true);
    expect(extractCalls([step({ calls: [call] })])?.[0].method).toBe("POST");
  });

  it("returns null when no call log step exists", () => {
    expect(extractCalls([])).toBeNull();
  });
});
