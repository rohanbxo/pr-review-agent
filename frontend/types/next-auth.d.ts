import type { DefaultSession } from "next-auth";

declare module "next-auth" {
  interface Session {
    /** Backend-issued JWT from POST /auth/github/exchange. */
    apiToken: string;
    /** Epoch milliseconds. */
    apiTokenExpiresAt: number;
    user: DefaultSession["user"];
  }
}

declare module "@auth/core/jwt" {
  interface JWT {
    apiToken?: string;
    apiTokenExpiresAt?: number;
  }
}
