"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/**
 * While a run is queued/running, re-render the server component every few seconds.
 * Data is re-fetched server-side, so the backend token never reaches browser JS.
 */
export function AutoRefresh({ active, intervalMs = 3000 }: { active: boolean; intervalMs?: number }) {
  const router = useRouter();
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") router.refresh();
    }, intervalMs);
    return () => window.clearInterval(id);
  }, [active, intervalMs, router]);
  return null;
}
