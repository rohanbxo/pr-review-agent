// Mirrors backend/app/validation.py exactly. The backend is still the authority; this only
// saves a round trip and gives the user an immediate, specific message.

// GitHub login rules: alphanumeric or hyphen, leading alphanumeric, max 39.
const OWNER = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/;
// Repos may start with a dot (.github is real); `.` and `..` are rejected separately.
const REPO = /^[A-Za-z0-9._-]{1,100}$/;

export type ValidationResult = { ok: true; value: string } | { ok: false; error: string };

export function validateRepoFullName(
  value: string,
  { allowWildcard = false }: { allowWildcard?: boolean } = {},
): ValidationResult {
  if (typeof value !== "string" || value.split("/").length !== 2) {
    return { ok: false, error: "repo must be 'owner/name'" };
  }
  const [owner, repo] = value.split("/");
  if (!OWNER.test(owner)) {
    return { ok: false, error: "invalid owner" };
  }
  if (repo === "*") {
    return allowWildcard
      ? { ok: true, value }
      : { ok: false, error: "wildcard not allowed here" };
  }
  if (repo === "." || repo === ".." || !REPO.test(repo)) {
    return { ok: false, error: "invalid repository name" };
  }
  return { ok: true, value };
}

export type PrNumberResult = { ok: true; value: number } | { ok: false; error: string };

export function validatePrNumber(raw: string): PrNumberResult {
  const s = raw.trim();
  if (!/^[1-9][0-9]{0,9}$/.test(s)) {
    return { ok: false, error: "PR number must be a positive integer" };
  }
  const n = Number(s);
  if (!Number.isSafeInteger(n) || n > 2_147_483_647) {
    return { ok: false, error: "PR number is too large" };
  }
  return { ok: true, value: n };
}
