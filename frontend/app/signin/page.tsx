import { AuthError } from "next-auth";
import { redirect } from "next/navigation";

import { signIn } from "@/auth";
import { Notice } from "@/components/notice";
import { Button } from "@/components/ui/button";

const MESSAGES: Record<string, string> = {
  AccessDenied: "Your GitHub account isn't authorised for this app.",
  SessionExpired: "Your session has ended. Sign in again.",
  OAuthCallbackError: "GitHub sign-in was cancelled or failed. Try again.",
};

function safeCallbackUrl(raw: string | undefined): string {
  if (!raw) return "/";
  try {
    // Only same-origin paths; strip any host the query string tried to smuggle in.
    const u = new URL(raw, "http://local");
    const path = `${u.pathname}${u.search}`;
    return path.startsWith("/") && !path.startsWith("//") && u.pathname !== "/signin"
      ? path
      : "/";
  } catch {
    return "/";
  }
}

export default async function SignInPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; callbackUrl?: string }>;
}) {
  const { error, callbackUrl } = await searchParams;
  const redirectTo = safeCallbackUrl(callbackUrl);
  const message = error ? (MESSAGES[error] ?? "Sign-in failed. Try again.") : null;

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center gap-6 px-4 py-12">
      <div>
        <h1 className="text-xl font-semibold">PR Review Agent</h1>
        <p className="mt-1 text-muted-foreground">
          Read-only reviews of GitHub pull requests, with a record of everything the agent read.
        </p>
      </div>

      {message ? <Notice role="alert" title={message} /> : null}

      <form
        action={async () => {
          "use server";
          try {
            await signIn("github", { redirectTo });
          } catch (e) {
            if (e instanceof AuthError) redirect(`/signin?error=${encodeURIComponent(e.type)}`);
            throw e;
          }
        }}
      >
        <Button type="submit" className="w-full">
          Sign in with GitHub
        </Button>
      </form>

      <p className="text-xs text-muted-foreground">
        Requests <span className="font-mono">read:user user:email read:org</span>. Never{" "}
        <span className="font-mono">repo</span>.
      </p>
    </main>
  );
}
