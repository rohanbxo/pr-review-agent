// Typed mirrors of CONTRACTS.md. Keep in sync with backend/app/routers/* and app/agent/schema.py.

export type Role = "admin" | "reviewer" | "viewer";

export type Permission =
  | "review:create"
  | "review:read"
  | "review:read_all"
  | "audit:read"
  | "user:manage"
  | "grant:manage";

export interface GrantOut {
  id: number | string;
  repo_full_name: string;
  created_at: string;
}

export interface UserOut {
  id: number | string;
  github_id: number;
  github_login: string;
  email: string | null;
  avatar_url: string | null;
  role: Role;
  is_active: boolean;
  last_login_at: string | null;
  grants: GrantOut[];
}

export interface MeOut extends UserOut {
  permissions: Permission[];
}

export interface ExchangeOut {
  token: string;
  expires_at: string;
  user: UserOut;
}

export type RunStatus = "queued" | "running" | "succeeded" | "failed";

export type Severity = "low" | "medium" | "high" | "critical";
export type Risk = "low" | "medium" | "high";

export interface LineRange {
  start: number;
  end: number;
}

export interface Finding {
  file: string;
  lines: LineRange;
  severity: Severity;
  title: string;
  detail: string;
  suggestion: string | null;
}

export interface ReviewResult {
  summary: string;
  risk: Risk;
  findings: Finding[];
  files_reviewed: string[];
}

export interface Usage {
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  [k: string]: unknown;
}

export interface ReviewRunOut {
  id: string;
  repo: string;
  pr_number: number;
  status: RunStatus;
  model: string | null;
  langfuse_trace_id: string | null;
  result: ReviewResult | null;
  usage: Usage | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  user_id: number | string;
}

export type StepKind = "node" | "github_calls" | "error";

export interface AgentStepOut {
  seq: number;
  kind: StepKind;
  name: string;
  input: unknown;
  output: unknown;
  latency_ms: number | null;
  created_at: string;
}

/** app/agent/github_client.py CallRecord.as_dict() */
export interface CallRecord {
  method: string;
  path: string;
  status: number | null;
  duration_ms: number;
  blocked: boolean;
  error: string | null;
}

export interface CreateReviewIn {
  repo: string;
  pr_number: number;
}
