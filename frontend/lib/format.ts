export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function formatDurationMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
}

export function formatRunDuration(start: string | null, end: string | null): string {
  if (!start || !end) return "—";
  const ms = Date.parse(end) - Date.parse(start);
  return Number.isFinite(ms) ? formatDurationMs(ms) : "—";
}

/** "12 min 5 s" style, for Retry-After. */
export function formatRetryAfter(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m} min ${rem} s` : `${m} min`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `${h} h ${mm} min` : `${h} h`;
}

export function githubPrFilesUrl(repo: string, pr: number): string {
  return `https://github.com/${repo}/pull/${pr}/files`;
}

export function githubPrUrl(repo: string, pr: number): string {
  return `https://github.com/${repo}/pull/${pr}`;
}
