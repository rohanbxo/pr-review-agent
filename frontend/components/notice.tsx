import type { ReactNode } from "react";

import { formatRetryAfter } from "@/lib/format";

/** Neutral, bordered message box. No hue — DESIGN.md rule 1. */
export function Notice({
  title,
  children,
  role = "status",
}: {
  title: string;
  children?: ReactNode;
  role?: "status" | "alert";
}) {
  return (
    <div role={role} className="rounded-md border-2 border-foreground/80 px-4 py-3">
      <p className="font-semibold">{title}</p>
      {children ? <div className="mt-1 text-muted-foreground">{children}</div> : null}
    </div>
  );
}

export function ApiErrorNotice({
  status,
  detail,
  retryAfter,
  action,
}: {
  status: number;
  detail: string;
  retryAfter: number | null;
  /** What the user was trying to do, e.g. "view this review". */
  action: string;
}) {
  if (status === 403) {
    return (
      <Notice role="alert" title={`You don't have permission to ${action}.`}>
        <p>Ask an admin for a role or repository grant that covers it.</p>
      </Notice>
    );
  }
  if (status === 429) {
    return (
      <Notice role="alert" title="Rate limit reached.">
        {retryAfter !== null ? (
          <p>
            Try again in <span className="font-mono">{formatRetryAfter(retryAfter)}</span>.
          </p>
        ) : (
          <p>Try again later.</p>
        )}
      </Notice>
    );
  }
  if (status === 404) {
    return <Notice role="alert" title="Not found." />;
  }
  return (
    <Notice role="alert" title={`Couldn't ${action}.`}>
      <p className="font-mono text-xs">
        HTTP {status}: {detail}
      </p>
    </Notice>
  );
}
