import "server-only";

import { redirect } from "next/navigation";

import { ApiError, NotSignedInError } from "./api";

/**
 * Normalise API failures for server components: a missing/expired/rejected backend token
 * sends the user back to /signin; everything else becomes a renderable error.
 */
export async function guarded<T>(
  fn: () => Promise<T>,
): Promise<{ ok: true; data: T } | { ok: false; error: ApiError }> {
  try {
    return { ok: true, data: await fn() };
  } catch (e) {
    if (e instanceof NotSignedInError) redirect("/signin");
    if (e instanceof ApiError) {
      if (e.status === 401) redirect("/signin?error=SessionExpired");
      return { ok: false, error: e };
    }
    throw e;
  }
}
