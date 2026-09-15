// The ONLY place severity hues are referenced (DESIGN.md: colour means severity and nothing
// else; severity renders as a left rule, never a filled chip). Class strings are literal so
// Tailwind can see them.

import type { Finding, Risk, Severity } from "./types";

export const SEVERITIES: readonly Severity[] = ["critical", "high", "medium", "low"] as const;

const RULE: Record<Severity, string> = {
  low: "border-l-4 border-l-severity-low",
  medium: "border-l-4 border-l-severity-medium",
  high: "border-l-4 border-l-severity-high",
  critical: "border-l-4 border-l-severity-critical",
};

const LABEL: Record<Severity, string> = {
  low: "LOW",
  medium: "MEDIUM",
  high: "HIGH",
  critical: "CRITICAL",
};

const RANK: Record<Severity, number> = { critical: 0, high: 1, medium: 2, low: 3 };

function normalise(s: string): Severity {
  return (s in RULE ? s : "low") as Severity;
}

/** Left-rule classes for a severity. Never returns a background/fill class. */
export function severityRuleClass(severity: Severity | Risk | string): string {
  return RULE[normalise(severity)];
}

/** Visible text label — severity is never conveyed by colour alone. */
export function severityLabel(severity: Severity | Risk | string): string {
  return LABEL[normalise(severity)];
}

export function compareFindings(a: Finding, b: Finding): number {
  return (
    RANK[normalise(a.severity)] - RANK[normalise(b.severity)] ||
    a.lines.start - b.lines.start ||
    a.lines.end - b.lines.end
  );
}

/** Group findings by file, preserving first-seen file order, each group sorted by severity. */
export function groupFindingsByFile(findings: Finding[]): Array<[string, Finding[]]> {
  const groups = new Map<string, Finding[]>();
  for (const f of findings) {
    const list = groups.get(f.file) ?? [];
    list.push(f);
    groups.set(f.file, list);
  }
  return [...groups.entries()].map(([file, list]) => [file, [...list].sort(compareFindings)]);
}

export function formatLineRange({ start, end }: { start: number; end: number }): string {
  return start === end ? `L${start}` : `L${start}–L${end}`;
}
