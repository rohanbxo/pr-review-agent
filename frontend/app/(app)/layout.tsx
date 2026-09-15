import Link from "next/link";

import { signOut } from "@/auth";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { guarded } from "@/lib/session-guard";

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const me = await guarded(() => api.me());

  return (
    <div className="min-h-screen">
      <header className="border-b">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-2">
          <Link href="/" className="font-semibold no-underline hover:underline">
            PR Review Agent
          </Link>
          <div className="flex items-center gap-3 text-sm">
            {me.ok ? (
              <>
                <span className="font-mono">{me.data.github_login}</span>
                <span
                  className="rounded border px-1.5 py-0.5 font-mono text-xs uppercase"
                  title="Your role"
                >
                  {me.data.role}
                </span>
              </>
            ) : (
              <span className="text-muted-foreground">Identity unavailable</span>
            )}
            <form
              action={async () => {
                "use server";
                await signOut({ redirectTo: "/signin" });
              }}
            >
              <Button type="submit" variant="outline" size="sm">
                Sign out
              </Button>
            </form>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
    </div>
  );
}
