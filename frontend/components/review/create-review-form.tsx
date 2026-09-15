"use client";

import { useActionState, useState } from "react";

import { createReviewAction, type CreateReviewState } from "@/app/(app)/actions";
import { ApiErrorNotice, Notice } from "@/components/notice";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { validatePrNumber, validateRepoFullName } from "@/lib/validation";

const INITIAL: CreateReviewState = { status: "idle" };

const INVALID = "aria-invalid:border-2 aria-invalid:border-foreground aria-invalid:ring-0";

export function CreateReviewForm() {
  const [state, formAction, pending] = useActionState(createReviewAction, INITIAL);
  const [repo, setRepo] = useState("");
  const [pr, setPr] = useState("");
  const [touched, setTouched] = useState({ repo: false, pr: false });

  const repoCheck = validateRepoFullName(repo.trim());
  const prCheck = validatePrNumber(pr);
  const serverFields = state.status === "error" ? state.fields : undefined;
  const repoError = touched.repo && !repoCheck.ok ? repoCheck.error : serverFields?.repo;
  const prError = touched.pr && !prCheck.ok ? prCheck.error : serverFields?.pr;

  return (
    <form
      action={formAction}
      noValidate
      onSubmit={(e) => {
        setTouched({ repo: true, pr: true });
        if (!repoCheck.ok || !prCheck.ok) e.preventDefault();
      }}
      className="space-y-3"
    >
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1 basis-64 space-y-1">
          <Label htmlFor="repo">Repository</Label>
          <Input
            id="repo"
            name="repo"
            placeholder="owner/name"
            autoComplete="off"
            spellCheck={false}
            className={`font-mono ${INVALID}`}
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, repo: true }))}
            aria-invalid={!!repoError}
            aria-describedby={repoError ? "repo-error" : undefined}
          />
          {repoError ? (
            <p id="repo-error" className="text-xs font-semibold">
              {repoError}
            </p>
          ) : null}
        </div>
        <div className="w-32 space-y-1">
          <Label htmlFor="pr_number">PR number</Label>
          <Input
            id="pr_number"
            name="pr_number"
            inputMode="numeric"
            placeholder="123"
            autoComplete="off"
            className={`font-mono ${INVALID}`}
            value={pr}
            onChange={(e) => setPr(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, pr: true }))}
            aria-invalid={!!prError}
            aria-describedby={prError ? "pr-error" : undefined}
          />
          {prError ? (
            <p id="pr-error" className="text-xs font-semibold">
              {prError}
            </p>
          ) : null}
        </div>
        <div className="space-y-1">
          <span className="block text-sm leading-none" aria-hidden>
            &nbsp;
          </span>
          <Button type="submit" disabled={pending}>
            {pending ? "Starting…" : "Start review"}
          </Button>
        </div>
      </div>

      <div aria-live="polite">
        {state.status === "error" && state.httpStatus !== null ? (
          <ApiErrorNotice
            status={state.httpStatus}
            detail={state.message}
            retryAfter={state.retryAfter}
            action="review this repository"
          />
        ) : state.status === "error" && !state.fields ? (
          <Notice role="alert" title={state.message} />
        ) : null}
      </div>
    </form>
  );
}
