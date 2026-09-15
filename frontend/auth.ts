import NextAuth, { AuthError } from "next-auth";
import GitHub from "next-auth/providers/github";

import type { ExchangeOut } from "@/lib/types";

const SESSION_MAX_AGE_S = 8 * 60 * 60;

/**
 * Raised when the backend refuses the exchange (403: unknown/deactivated account).
 * `type = "AccessDenied"` is one of Auth.js's client-safe error types, so the user is
 * redirected to `/signin?error=AccessDenied` rather than a generic Configuration error.
 */
class NotAuthorisedForApp extends AuthError {
  static type = "AccessDenied" as const;
}

async function exchangeGitHubToken(accessToken: string): Promise<ExchangeOut> {
  const base = process.env.API_INTERNAL_URL?.replace(/\/+$/, "");
  const secret = process.env.AUTH_BRIDGE_SECRET;
  if (!base || !secret) {
    throw new Error("API_INTERNAL_URL and AUTH_BRIDGE_SECRET must be set");
  }
  const res = await fetch(`${base}/auth/github/exchange`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      "X-Auth-Bridge-Secret": secret,
    },
    // The access token ONLY. The backend re-reads identity from GitHub itself and never
    // trusts a profile sent to it.
    body: JSON.stringify({ access_token: accessToken }),
    cache: "no-store",
  });
  if (res.status === 403) {
    throw new NotAuthorisedForApp("backend refused the GitHub account");
  }
  if (!res.ok) {
    throw new Error(`token exchange failed with HTTP ${res.status}`);
  }
  return (await res.json()) as ExchangeOut;
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [
    GitHub({
      // Never `repo` — that scope is read-write.
      authorization: { params: { scope: "read:user user:email read:org" } },
    }),
  ],
  session: { strategy: "jwt", maxAge: SESSION_MAX_AGE_S },
  pages: { signIn: "/signin", error: "/signin" },
  trustHost: true,
  callbacks: {
    async jwt({ token, account }) {
      if (account) {
        // Initial sign-in: exchange server-to-server for a backend JWT.
        if (!account.access_token) {
          throw new NotAuthorisedForApp("GitHub returned no access token");
        }
        const exchanged = await exchangeGitHubToken(account.access_token);
        const expiresAt = Date.parse(exchanged.expires_at);
        return {
          // Keep only what the UI needs; drop the GitHub access token from the cookie.
          sub: String(exchanged.user.id),
          name: exchanged.user.github_login,
          email: exchanged.user.email,
          picture: exchanged.user.avatar_url,
          apiToken: exchanged.token,
          apiTokenExpiresAt: Number.isNaN(expiresAt) ? 0 : expiresAt,
        };
      }
      // No refresh (out of scope): once the backend token expires, the session is over.
      if (!token.apiToken || !token.apiTokenExpiresAt || Date.now() >= token.apiTokenExpiresAt) {
        return null;
      }
      return token;
    },
    async session({ session, token }) {
      session.apiToken = token.apiToken as string;
      session.apiTokenExpiresAt = token.apiTokenExpiresAt as number;
      return session;
    },
    authorized({ auth: session, request }) {
      const { pathname } = request.nextUrl;
      if (pathname === "/signin" || pathname.startsWith("/api/auth/")) return true;
      return !!session?.apiToken;
    },
  },
});
