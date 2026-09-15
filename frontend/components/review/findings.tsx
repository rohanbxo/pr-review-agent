import { createHash } from "node:crypto";

import { formatLineRange, groupFindingsByFile, severityLabel, severityRuleClass } from "@/lib/severity";
import { githubPrFilesUrl } from "@/lib/format";
import type { Finding, ReviewResult } from "@/lib/types";

/** GitHub's PR "Files changed" anchor: #diff-<sha256(path)>R<line>. */
function diffAnchor(file: string, line?: number): string {
  const hash = createHash("sha256").update(file).digest("hex");
  return `#diff-${hash}${line ? `R${line}` : ""}`;
}

function FindingBlock({ finding, repo, pr }: { finding: Finding; repo: string; pr: number }) {
  const range = formatLineRange(finding.lines);
  return (
    <li
      data-severity={finding.severity}
      className={`${severityRuleClass(finding.severity)} rounded-r-md border-y border-r py-3 pr-4 pl-4`}
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-xs font-bold tracking-wide">{severityLabel(finding.severity)}</span>
        <h4 className="font-semibold">{finding.title}</h4>
        <a
          href={`${githubPrFilesUrl(repo, pr)}${diffAnchor(finding.file, finding.lines.start)}`}
          target="_blank"
          rel="noreferrer"
          className="font-mono text-xs"
          aria-label={`${finding.file} lines ${range} on GitHub`}
        >
          {range}
        </a>
      </div>
      <p className="mt-2 whitespace-pre-wrap">{finding.detail}</p>
      {finding.suggestion ? (
        <div className="mt-3 border-t pt-2">
          <p className="text-xs font-semibold">Suggestion</p>
          <p className="mt-1 whitespace-pre-wrap">{finding.suggestion}</p>
        </div>
      ) : null}
    </li>
  );
}

export function Findings({ result, repo, pr }: { result: ReviewResult; repo: string; pr: number }) {
  const findings = result.findings ?? [];
  const filesReviewed = result.files_reviewed ?? [];

  if (findings.length === 0) {
    return (
      <section aria-labelledby="findings-heading" className="rounded-md border-2 border-foreground/80 px-4 py-4">
        <h2 id="findings-heading" className="text-base font-semibold">
          No issues found
        </h2>
        <p className="mt-1">
          The agent reviewed{" "}
          <span className="font-mono">{filesReviewed.length}</span>{" "}
          {filesReviewed.length === 1 ? "file" : "files"} and found nothing worth flagging.
        </p>
        <p className="mt-1 text-muted-foreground">
          The files it read are listed under “What the agent read” below.
        </p>
      </section>
    );
  }

  const groups = groupFindingsByFile(findings);
  return (
    <section aria-labelledby="findings-heading" className="space-y-5">
      <h2 id="findings-heading" className="text-sm font-semibold">
        Findings <span className="font-mono">({findings.length})</span>
      </h2>
      {groups.map(([file, list]) => (
        <div key={file} className="space-y-2">
          <div className="flex flex-wrap items-baseline justify-between gap-2 border-b pb-1">
            <h3 className="font-mono text-sm font-semibold break-all">{file}</h3>
            <span className="flex items-baseline gap-3 text-xs">
              <span className="text-muted-foreground">
                {list.length} {list.length === 1 ? "finding" : "findings"}
              </span>
              <a href={`${githubPrFilesUrl(repo, pr)}${diffAnchor(file)}`} target="_blank" rel="noreferrer">
                View in PR
              </a>
            </span>
          </div>
          <ul className="space-y-2">
            {list.map((f, i) => (
              <FindingBlock key={`${f.lines.start}-${f.lines.end}-${i}`} finding={f} repo={repo} pr={pr} />
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}
