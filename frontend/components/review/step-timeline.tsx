import { formatDurationMs, formatTimestamp } from "@/lib/format";
import type { AgentStepOut } from "@/lib/types";

export function StepTimeline({ steps }: { steps: AgentStepOut[] }) {
  const ordered = [...steps].sort((a, b) => a.seq - b.seq);
  return (
    <section aria-labelledby="timeline" className="space-y-2">
      <h2 id="timeline" className="text-sm font-semibold">
        Step timeline
      </h2>
      {ordered.length === 0 ? (
        <p className="text-muted-foreground">No steps recorded yet.</p>
      ) : (
        <ol className="border-l">
          {ordered.map((s) => (
            <li key={s.seq} className="relative py-1.5 pl-4">
              <span aria-hidden className="absolute top-3 -left-[3px] size-1.5 rounded-full bg-foreground" />
              <div className="flex flex-wrap items-baseline gap-x-3 text-xs">
                <span className="font-mono text-muted-foreground">#{s.seq}</span>
                <span className={`font-mono ${s.kind === "error" ? "font-bold" : "font-semibold"}`}>
                  {s.name}
                </span>
                <span className="font-mono text-muted-foreground">
                  {s.kind === "error" ? "ERROR" : s.kind}
                </span>
                <span className="font-mono">{formatDurationMs(s.latency_ms)}</span>
                <span className="font-mono text-muted-foreground">{formatTimestamp(s.created_at)}</span>
              </div>
              {s.kind === "error" && s.output ? (
                <pre className="mt-1 overflow-x-auto rounded border px-2 py-1 font-mono text-xs whitespace-pre-wrap">
                  {typeof s.output === "string" ? s.output : JSON.stringify(s.output, null, 2)}
                </pre>
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
