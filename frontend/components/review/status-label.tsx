import type { RunStatus } from "@/lib/types";

/** Run status as neutral monospace text. Never coloured — status is not severity. */
export function StatusLabel({ status }: { status: RunStatus }) {
  return (
    <span
      className={`inline-block rounded border px-1.5 py-0.5 font-mono text-xs ${
        status === "failed" ? "border-foreground font-bold" : ""
      }`}
    >
      {status}
    </span>
  );
}
