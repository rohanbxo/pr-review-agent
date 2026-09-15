import type { AgentStepOut, CallRecord } from "./types";

/**
 * Extract the GitHub call log from the `github_calls` step. The runner persists the
 * CallRecord list as the step output; accept either a bare list or `{calls: [...]}`.
 */
export function extractCalls(steps: AgentStepOut[]): CallRecord[] | null {
  const step = steps.find((s) => s.kind === "github_calls");
  if (!step) return null;
  const out = step.output as unknown;
  const list = Array.isArray(out)
    ? out
    : out && typeof out === "object" && Array.isArray((out as { calls?: unknown }).calls)
      ? (out as { calls: unknown[] }).calls
      : [];
  return list
    .filter((c): c is Record<string, unknown> => !!c && typeof c === "object")
    .map((c) => ({
      method: String(c.method ?? ""),
      path: String(c.path ?? ""),
      status: typeof c.status === "number" ? c.status : null,
      duration_ms: typeof c.duration_ms === "number" ? c.duration_ms : 0,
      blocked: Boolean(c.blocked),
      error: typeof c.error === "string" ? c.error : null,
    }));
}
