"use server";

import { redirect } from "next/navigation";

import { api, ApiError, NotSignedInError } from "@/lib/api";
import { validatePrNumber, validateRepoFullName } from "@/lib/validation";

export type CreateReviewState =
  | { status: "idle" }
  | {
      status: "error";
      httpStatus: number | null;
      message: string;
      retryAfter: number | null;
      fields?: { repo?: string; pr?: string };
    };

export async function createReviewAction(
  _prev: CreateReviewState,
  formData: FormData,
): Promise<CreateReviewState> {
  const repoRaw = String(formData.get("repo") ?? "").trim();
  const prRaw = String(formData.get("pr_number") ?? "");

  // Same rules as the client and backend/app/validation.py; the backend re-checks.
  const repo = validateRepoFullName(repoRaw);
  const pr = validatePrNumber(prRaw);
  if (!repo.ok || !pr.ok) {
    return {
      status: "error",
      httpStatus: null,
      message: "Fix the highlighted fields.",
      retryAfter: null,
      fields: { repo: repo.ok ? undefined : repo.error, pr: pr.ok ? undefined : pr.error },
    };
  }

  let id: string;
  try {
    const run = await api.createReview({ repo: repo.value, pr_number: pr.value });
    id = run.id;
  } catch (e) {
    if (e instanceof NotSignedInError) redirect("/signin");
    if (e instanceof ApiError) {
      if (e.status === 401) redirect("/signin?error=SessionExpired");
      return {
        status: "error",
        httpStatus: e.status,
        message: e.detail,
        retryAfter: e.retryAfter,
      };
    }
    throw e;
  }
  redirect(`/reviews/${encodeURIComponent(id)}`);
}
