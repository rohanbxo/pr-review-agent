import "server-only";

import { cache } from "react";

import { auth } from "@/auth";
import type {
  AgentStepOut,
  CreateReviewIn,
  MeOut,
  ReviewRunOut,
} from "./types";

// Server-side only. The browser never talks to FastAPI with the backend token: server
// components, server actions and route handlers call API_INTERNAL_URL directly.

export function apiBaseUrl(): string {
  const url = process.env.API_INTERNAL_URL;
  if (!url) throw new Error("API_INTERNAL_URL is not set");
  return url.replace(/\/+$/, "");
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
    /** Seconds, from the Retry-After header on 429. */
    public readonly retryAfter: number | null = null,
  ) {
    super(`API ${status}: ${detail}`);
    this.name = "ApiError";
  }
}

/** Thrown when there is no usable backend token (signed out or expired). */
export class NotSignedInError extends Error {
  constructor() {
    super("not signed in");
    this.name = "NotSignedInError";
  }
}

function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  const secs = Number(value);
  if (Number.isFinite(secs)) return Math.max(0, Math.ceil(secs));
  const date = Date.parse(value);
  return Number.isNaN(date) ? null : Math.max(0, Math.ceil((date - Date.now()) / 1000));
}

async function detailOf(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (body.detail !== undefined) return JSON.stringify(body.detail);
  } catch {
    // non-JSON error body
  }
  return res.statusText || "request failed";
}

async function request<T>(
  path: string,
  init: RequestInit & { token: string },
): Promise<T> {
  const { token, headers, ...rest } = init;
  const res = await fetch(`${apiBaseUrl()}${path}`, {
    ...rest,
    headers: {
      Accept: "application/json",
      ...(rest.body ? { "Content-Type": "application/json" } : {}),
      ...headers,
      Authorization: `Bearer ${token}`,
    },
    cache: "no-store",
  });
  if (!res.ok) {
    throw new ApiError(
      res.status,
      await detailOf(res),
      parseRetryAfter(res.headers.get("Retry-After")),
    );
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

async function token(): Promise<string> {
  const session = await auth();
  if (!session?.apiToken) throw new NotSignedInError();
  return session.apiToken;
}

export const api = {
  /** Deduplicated per request (layout + page both need it). */
  me: cache(async (): Promise<MeOut> => request<MeOut>("/auth/me", { token: await token() })),
  async listReviews(limit = 20): Promise<ReviewRunOut[]> {
    return request<ReviewRunOut[]>(`/reviews?limit=${encodeURIComponent(limit)}`, {
      token: await token(),
    });
  },
  async getReview(id: string): Promise<ReviewRunOut> {
    return request<ReviewRunOut>(`/reviews/${encodeURIComponent(id)}`, {
      token: await token(),
    });
  },
  async getSteps(id: string): Promise<AgentStepOut[]> {
    return request<AgentStepOut[]>(`/reviews/${encodeURIComponent(id)}/steps`, {
      token: await token(),
    });
  },
  async createReview(body: CreateReviewIn): Promise<ReviewRunOut> {
    return request<ReviewRunOut>("/reviews", {
      method: "POST",
      body: JSON.stringify(body),
      token: await token(),
    });
  },
};
