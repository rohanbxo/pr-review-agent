import { notFound } from "next/navigation";

import { ApiErrorNotice, Notice } from "@/components/notice";
import { AgentReadPanel } from "@/components/review/agent-read-panel";
import { AutoRefresh } from "@/components/review/auto-refresh";
import { Findings } from "@/components/review/findings";
import { StatusLabel } from "@/components/review/status-label";
import { StepTimeline } from "@/components/review/step-timeline";
import { api } from "@/lib/api";
import { extractCalls } from "@/lib/calls";
import { formatRunDuration, formatTimestamp, githubPrUrl } from "@/lib/format";
import { severityLabel, severityRuleClass } from "@/lib/severity";
import { guarded } from "@/lib/session-guard";

export const dynamic = "force-dynamic";

export default async function ReviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const [runRes, stepsRes] = await Promise.all([
    guarded(() => api.getReview(id)),
    guarded(() => api.getSteps(id)),
  ]);

  if (!runRes.ok) {
    if (runRes.error.status === 404 || runRes.error.status === 422) notFound();
    return (
      <ApiErrorNotice
        status={runRes.error.status}
        detail={runRes.error.detail}
        retryAfter={runRes.error.retryAfter}
        action="view this review"
      />
    );
  }

  const run = runRes.data;
  const inProgress = run.status === "queued" || run.status === "running";
  const steps = stepsRes.ok ? stepsRes.data : [];
  const calls = stepsRes.ok ? extractCalls(stepsRes.data) : null;
  const result = run.status === "succeeded" ? run.result : null;

  return (
    <div className="space-y-6">
      <AutoRefresh active={inProgress} />

      <div className="space-y-1">
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 className="font-mono text-xl font-semibold break-all">
            <a href={githubPrUrl(run.repo, run.pr_number)} target="_blank" rel="noreferrer">
              {run.repo}#{run.pr_number}
            </a>
          </h1>
          <StatusLabel status={run.status} />
        </div>
        <p className="font-mono text-xs text-muted-foreground">
          created {formatTimestamp(run.created_at)} · duration{" "}
          {formatRunDuration(run.started_at, run.finished_at)}
          {run.model ? ` · ${run.model}` : ""}
          {run.usage?.total_tokens !== undefined ? ` · ${run.usage.total_tokens} tokens` : ""}
        </p>
      </div>

      <div aria-live="polite">
        {inProgress ? (
          <Notice title={run.status === "queued" ? "Queued." : "Reviewing…"}>
            <p>This page checks again every few seconds.</p>
          </Notice>
        ) : run.status === "failed" ? (
          <Notice role="alert" title="Review failed.">
            {run.error ? <p className="font-mono text-xs break-all">{run.error}</p> : null}
          </Notice>
        ) : null}
      </div>

      {result ? (
        <>
          <section
            aria-labelledby="summary"
            data-risk={result.risk}
            className={`${severityRuleClass(result.risk)} rounded-r-md border-y border-r py-3 pr-4 pl-4`}
          >
            <div className="flex flex-wrap items-baseline gap-3">
              <h2 id="summary" className="text-sm font-semibold">
                Summary
              </h2>
              <span className="text-xs">
                Overall risk: <span className="font-bold tracking-wide">{severityLabel(result.risk)}</span>
              </span>
            </div>
            <p className="mt-2 whitespace-pre-wrap">{result.summary}</p>
          </section>

          <Findings result={result} repo={run.repo} pr={run.pr_number} />
        </>
      ) : null}

      <AgentReadPanel
        filesReviewed={result ? (result.files_reviewed ?? []) : null}
        calls={calls}
        stepsError={
          stepsRes.ok ? undefined : (
            <ApiErrorNotice
              status={stepsRes.error.status}
              detail={stepsRes.error.detail}
              retryAfter={stepsRes.error.retryAfter}
              action="view this review's steps"
            />
          )
        }
      />

      {stepsRes.ok ? <StepTimeline steps={steps} /> : null}
    </div>
  );
}
