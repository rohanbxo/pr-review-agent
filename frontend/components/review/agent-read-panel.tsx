import { formatDurationMs } from "@/lib/format";
import type { CallRecord } from "@/lib/types";

export function AgentReadPanel({
  filesReviewed,
  calls,
  stepsError,
}: {
  filesReviewed: string[] | null;
  calls: CallRecord[] | null;
  stepsError?: React.ReactNode;
}) {
  const blocked = calls?.filter((c) => c.blocked).length ?? 0;

  return (
    <section aria-labelledby="agent-read" className="space-y-4 rounded-md border px-4 py-4">
      <h2 id="agent-read" className="text-sm font-semibold">
        What the agent read
      </h2>

      <div className="space-y-2">
        <h3 className="text-xs font-semibold">
          Files reviewed{" "}
          {filesReviewed ? <span className="font-mono">({filesReviewed.length})</span> : null}
        </h3>
        {filesReviewed === null ? (
          <p className="text-muted-foreground">Available when the review finishes.</p>
        ) : filesReviewed.length === 0 ? (
          <p className="text-muted-foreground">The agent did not record any files.</p>
        ) : (
          <ul className="space-y-0.5 font-mono text-xs">
            {filesReviewed.map((f) => (
              <li key={f} className="break-all">
                {f}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="space-y-2">
        <h3 className="text-xs font-semibold">GitHub API calls</h3>
        {stepsError ? (
          stepsError
        ) : calls === null ? (
          <p className="text-muted-foreground">
            The call log is written when the run ends, whatever the outcome.
          </p>
        ) : (
          <>
            <p className="text-xs">
              <span className="font-mono">{calls.length}</span> {calls.length === 1 ? "call" : "calls"},{" "}
              <span className={`font-mono ${blocked ? "font-bold" : ""}`}>{blocked}</span> blocked
              {blocked ? " by the read-only client (never sent to GitHub)" : ""}.
            </p>
            {calls.length > 0 ? (
              <div className="overflow-x-auto rounded-md border">
                <table className="w-full text-xs">
                  <thead className="border-b text-left text-muted-foreground">
                    <tr>
                      <th scope="col" className="px-3 py-1.5 font-medium">Method</th>
                      <th scope="col" className="px-3 py-1.5 font-medium">Path</th>
                      <th scope="col" className="px-3 py-1.5 font-medium">Status</th>
                      <th scope="col" className="px-3 py-1.5 text-right font-medium">Duration</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {calls.map((c, i) => (
                      <tr
                        key={i}
                        data-blocked={c.blocked || undefined}
                        className={`border-b last:border-b-0 ${
                          c.blocked ? "border-l-4 border-l-foreground font-semibold" : ""
                        }`}
                      >
                        <td className="px-3 py-1.5">{c.method}</td>
                        <td className="px-3 py-1.5 break-all">
                          {c.path}
                          {c.error ? (
                            <span className="block font-sans font-normal text-muted-foreground">
                              {c.error}
                            </span>
                          ) : null}
                        </td>
                        <td className="px-3 py-1.5">
                          {c.blocked ? <span className="font-bold">BLOCKED</span> : (c.status ?? "—")}
                        </td>
                        <td className="px-3 py-1.5 text-right">{formatDurationMs(c.duration_ms)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </>
        )}
      </div>
    </section>
  );
}
