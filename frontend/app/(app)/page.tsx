import Link from "next/link";

import { ApiErrorNotice } from "@/components/notice";
import { CreateReviewForm } from "@/components/review/create-review-form";
import { StatusLabel } from "@/components/review/status-label";
import { api } from "@/lib/api";
import { formatTimestamp } from "@/lib/format";
import { guarded } from "@/lib/session-guard";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  const [me, runs] = await Promise.all([guarded(() => api.me()), guarded(() => api.listReviews(25))]);
  const canCreate = me.ok && me.data.permissions.includes("review:create");

  return (
    <div className="space-y-8">
      {canCreate ? (
        <section aria-labelledby="new-review" className="space-y-3">
          <h1 id="new-review" className="text-xl font-semibold">
            Review a pull request
          </h1>
          <CreateReviewForm />
        </section>
      ) : (
        <section className="space-y-1">
          <h1 className="text-xl font-semibold">Reviews</h1>
          {me.ok ? (
            <p className="text-muted-foreground">
              Your role (<span className="font-mono">{me.data.role}</span>) can read reviews but
              not start them.
            </p>
          ) : null}
        </section>
      )}

      <section aria-labelledby="recent" className="space-y-3">
        <h2 id="recent" className="text-sm font-semibold">
          Recent reviews
        </h2>
        {!runs.ok ? (
          <ApiErrorNotice
            status={runs.error.status}
            detail={runs.error.detail}
            retryAfter={runs.error.retryAfter}
            action="list reviews"
          />
        ) : runs.data.length === 0 ? (
          <p className="text-muted-foreground">
            No reviews yet.{canCreate ? " Start one above." : ""}
          </p>
        ) : (
          <div className="overflow-x-auto rounded-md border">
            <table className="w-full text-sm">
              <thead className="border-b text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">Pull request</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Findings</th>
                  <th className="px-3 py-2 font-medium">Created</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.map((run) => (
                  <tr key={run.id} className="border-b last:border-b-0">
                    <td className="px-3 py-2">
                      <Link href={`/reviews/${run.id}`} className="font-mono">
                        {run.repo}#{run.pr_number}
                      </Link>
                    </td>
                    <td className="px-3 py-2">
                      <StatusLabel status={run.status} />
                    </td>
                    <td className="px-3 py-2 font-mono">
                      {run.status === "succeeded" && run.result
                        ? run.result.findings.length
                        : "—"}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-muted-foreground">
                      {formatTimestamp(run.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
