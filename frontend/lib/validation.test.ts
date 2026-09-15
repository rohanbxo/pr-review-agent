import { describe, expect, it } from "vitest";

import { validatePrNumber, validateRepoFullName } from "./validation";

describe("validateRepoFullName (mirrors backend/app/validation.py)", () => {
  it.each(["psf/requests", "octo-org/.github", "a/b", "A1/x_y.z-w", `${"a".repeat(39)}/r`])(
    "accepts %s",
    (v) => {
      expect(validateRepoFullName(v)).toEqual({ ok: true, value: v });
    },
  );

  it.each([
    ["../etc", "invalid owner"],
    ["owner/..", "invalid repository name"],
    ["owner/.", "invalid repository name"],
    ["-owner/repo", "invalid owner"],
    ["own_er/repo", "invalid owner"],
    [`${"a".repeat(40)}/r`, "invalid owner"],
    ["owner/re po", "invalid repository name"],
    ["owner/", "invalid repository name"],
    ["owner", "repo must be 'owner/name'"],
    ["a/b/c", "repo must be 'owner/name'"],
    ["owner/*", "wildcard not allowed here"],
  ])("rejects %s", (v, error) => {
    expect(validateRepoFullName(v)).toEqual({ ok: false, error });
  });

  it("allows a wildcard only when asked", () => {
    expect(validateRepoFullName("owner/*", { allowWildcard: true }).ok).toBe(true);
  });
});

describe("validatePrNumber", () => {
  it("accepts positive integers", () => {
    expect(validatePrNumber(" 42 ")).toEqual({ ok: true, value: 42 });
  });
  it.each(["0", "-1", "1.5", "abc", "", "01", "99999999999"])("rejects %s", (v) => {
    expect(validatePrNumber(v).ok).toBe(false);
  });
});
