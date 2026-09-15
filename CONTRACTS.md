# CONTRACTS.md — shared interfaces between workstreams

SPEC.md is the source of truth for *what*. This file fixes the *interfaces* so parallel
workstreams can build against each other. If you must change a contract, keep it
backward-compatible and note it at the bottom under "Changes".

## Environment

- Backend venv: `backend/.venv` (Python 3.12, deps from `backend/requirements-dev.txt` installed).
  Run tests: `cd backend && .venv/Scripts/python.exe -m pytest -q`.
- Docker is available locally. There is **no** GitHub token and **no** LLM API key on this machine:
  anything requiring them must be built, unit-tested offline, and marked `@pytest.mark.live`.
- LLM provider: `settings.llm_provider` — `openai_compatible` (default; `ChatOpenAI` against
  `LLM_BASE_URL`, default OpenRouter) or `anthropic` (`ChatAnthropic`). Model from `AGENT_MODEL`
  (alias `LLM_MODEL`). Constructed ONLY in `app/agent/llm.py`.
- Langfuse Python SDK installed is **v4.x** — check its actual API (`langfuse.langchain.CallbackHandler`,
  trace attributes) in `.venv` before using it; do not assume v2 APIs.

## Foundation (already written — do not restructure; small additive fixes OK)

| Module | What |
|---|---|
| `app/config.py` | `get_settings()` → `Settings` (all env config) |
| `app/db.py` | `Base`, `JSONType` (JSONB w/ SQLite variant), `get_session` dep, `get_sessionmaker()`, `set_sessionmaker()` |
| `app/models.py` | `User`, `RepoGrant`, `ReviewRun`, `AgentStep`, `AuditLog`; enums `Role`, `RunStatus`, `StepKind{node,github_calls,error}`, `Outcome{success,denied,error}` |
| `app/rbac.py` | `Permission` enum, `ROLE_PERMISSIONS`, `has_permission(role, perm)`, `permissions_for(role)` |
| `app/validation.py` | `validate_repo_full_name(v, allow_wildcard=False)`, `grant_covers(grant, repo)` |
| `app/security.py` | `mint_jwt(user_id) -> (token, exp)`, `decode_jwt(token) -> user_id`, `encrypt_token`, `decrypt_token`, `TokenError` |
| `app/netutil.py` | `parse_networks`, `resolve_client_ip(peer, xff, trusted)` |
| `app/request_context.py` | `RequestContext(ip, user_agent, request_id)`, `get_request_context` dep |
| `app/audit.py` | `record_audit(session, *, ctx, actor, action, outcome, resource_type, resource_id, metadata)` — **never commits** |
| `app/deps.py` | `get_current_user` (Bearer backend JWT, DB-loaded, active check, general rate limit), `require(Permission)` (audits + 403 on denial) |
| `app/redis_client.py` | `get_redis` dep (tests override with fakeredis) |
| `app/main.py` | `create_app()`; includes routers `health, auth, admin, audit, reviews`. nginx strips `/api`. |
| `tests/conftest.py` | fixtures: `engine`, `sessionmaker`, `session`, `redis` (fakeredis), `app`, `client` (peer IP 203.0.113.7), `make_user(role, login=None, **kw)`, `auth_headers(user)`. Option `--run-live`, marker `live`. **Do not edit**; add fixtures in your own test module or `tests/helpers/<area>.py`. |

## HTTP API (paths as FastAPI sees them; browser uses `/api` prefix)

- `GET /healthz`
- `POST /auth/github/exchange` — header `X-Auth-Bridge-Secret`; body `{"access_token": str}` ONLY.
  → `200 {"token": str, "expires_at": iso8601, "user": UserOut}`; 401 bad secret; 403 unknown/unauthorised account.
- `GET /auth/me` → `UserOut` + `"permissions": [str]`
- `GET /admin/users` (user:manage) → `[UserOut]`
- `PATCH /admin/users/{id}` (user:manage) body `{"role"?: Role, "is_active"?: bool}` → `UserOut`. 409 last-admin guard / self-demote.
- `POST /admin/users/{id}/grants` (grant:manage) body `{"repo_full_name": "owner/repo"|"owner/*"}` → 201 `GrantOut`
- `DELETE /admin/users/{id}/grants/{grant_id}` (grant:manage) → 204
- `GET /audit?limit=&before_id=&action=` (audit:read) → `[AuditOut]`
- `POST /reviews` (review:create) body `{"repo": "owner/repo", "pr_number": int}` → `202 ReviewRunOut`;
  checks grant (`grant_covers`) → `can_read_repo` → per-user review quota (429 + `Retry-After`) → create run + audit in one commit → `BackgroundTasks.add_task(run_review, run.id)`.
- `GET /reviews?limit=` (review:read) → own runs + runs on repos covered by the user's grants; all runs with review:read_all.
- `GET /reviews/{id}` (review:read, same visibility) → `ReviewRunOut` incl. `result`, `usage`, `error`
- `GET /reviews/{id}/steps` → `[{seq, kind, name, input, output, latency_ms, created_at}]`

`UserOut = {id, github_id, github_login, email, avatar_url, role, is_active, last_login_at, grants: [GrantOut]}`
`GrantOut = {id, repo_full_name, created_at}`
`ReviewRunOut = {id, repo, pr_number, status, model, langfuse_trace_id, result, usage, error, created_at, started_at, finished_at, user_id}`

## Identity / credentials (workstream A)

- `app/github_identity.py` — user trust domain. Uses the **user's** OAuth token.
  - `async def fetch_identity(access_token, *, transport=None) -> GitHubIdentity(id, login, email, avatar_url, orgs: list[str])` (`/user`, `/user/emails`, `/user/orgs`)
  - `async def can_read_repo(*, user_login: str, repo_full_name: str, transport=None) -> bool` —
    asks GitHub whether THIS user can read THIS repo (public repo → True; else the App installation token
    on `GET /repos/{o}/{r}/collaborators/{login}/permission`, permission ≠ none → True). Fail closed on errors.
- `app/github_app.py` — mints the GitHub App installation token (JWT → `POST /app/installations/{id}/access_tokens`).
  This POST lives here, NOT in the read-only agent client. Falls back to `settings.github_readonly_token` in dev.
  - `async def get_installation_token(*, transport=None) -> str | None`

## Agent (workstream B)

- `app/agent/github_client.py`
  - `class ReadOnlyViolation(Exception)`
  - `@dataclass CallRecord(method, path, status: int|None, duration_ms: int, blocked: bool, error: str|None)` + `.as_dict()`
  - `class ReadOnlyGitHubClient(token: str|None, *, base_url=settings.github_api_url, transport: httpx.AsyncBaseTransport|None=None, max_bytes=settings.github_fetch_max_bytes)`
    - `.calls: list[CallRecord]`; `async request(method, path, params=None) -> httpx.Response` (raises `ReadOnlyViolation` BEFORE any socket for non GET/HEAD or off-allowlist path; the attempt is still recorded with `blocked=True`)
    - `async get_json(path, params=None)`, `async get_text(path, params=None, accept=...) -> str` (truncated to max_bytes), `async aclose()`, async context manager.
- `app/agent/schema.py` — pydantic:
  - `LineRange{start:int>=1, end:int>=start}`
  - `Finding{file:str, lines:LineRange, severity: "low"|"medium"|"high"|"critical", title, detail, suggestion: str|None}`
  - `ReviewResult{summary:str, risk:"low"|"medium"|"high", findings:list[Finding], files_reviewed:list[str]}`
- `app/agent/graph.py`
  - `async def review_pull_request(*, repo: str, pr_number: int, client: ReadOnlyGitHubClient, llm: BaseChatModel, callbacks: list|None=None, metadata: dict|None=None, on_step: Callable[[StepEvent], Awaitable[None]]|None=None) -> ReviewOutcome`
  - `StepEvent{name: str, input: dict|None, output: dict|None, latency_ms: int}`
  - `ReviewOutcome{result: ReviewResult, usage: {"input_tokens","output_tokens","total_tokens"}, calls: list[CallRecord], duration_s: float}`
- `app/agent/llm.py` — `resolve_llm_config(*, provider=None, model=None, require_key=True) -> LLMConfig`
  (raises `LLMConfigError`), `build_llm(LLMConfig) -> BaseChatModel`, `get_llm()` (= both, from settings),
  `structured_output(llm, schema)` (forced function calling on every provider), `configured_model_label()`.
  `ReviewOutcome.parse_failures` / `synthesis_attempts`; `SynthesisError.parse_failures` / `.attempts`.
- `app/agent/fixtures.py` — `def mock_transport_for_case(case: dict) -> httpx.MockTransport` serving a dataset case (below) at the same GitHub REST paths the tools use. Eval and injection tests go through the REAL client + allowlist with this transport.
- `app/agent/runner.py` — `async def run_review(run_id: uuid.UUID) -> None`: opens its own session via `app.db.get_sessionmaker()`, marks running, gets token via `github_app.get_installation_token()`, streams graph, persists each node as an `agent_steps` row (`kind=node`), and ALWAYS (finally) persists the call log as `kind=github_calls`. Sets `result`, `usage`, `status`, `langfuse_trace_id`, `finished_at`.

## Rate limiting / proxy (workstream C)

- `app/ratelimit.py` (replace stub, keep signatures): `SlidingWindowLimiter(redis).hit(key, limit, window_seconds) -> LimitResult(allowed, remaining, retry_after)`; `enforce_general_limit(user_id, redis)`; plus `async def enforce_review_quota(user: User, redis, session, ctx) -> None` (audits exhaustion, commits the audit row, raises 429 with `Retry-After`).
- `app/routers/reviews.py` as specified above.

## Dataset case format (workstreams B, E) — one JSON object per JSONL line

```json
{
  "id": "requests-6093-flip-cmp-1",
  "split": "injected | reverted | clean | injection",
  "repo": "psf/requests",
  "pr_number": 6093,
  "title": "...", "body": "...",
  "base_sha": "...", "head_sha": "...",
  "files": [
    {"filename": "src/x.py", "status": "modified", "additions": 3, "deletions": 1,
     "patch": "@@ -10,4 +10,6 @@ ...", "content": "<full file at head, optional>"}
  ],
  "review_comments": [{"path": "...", "line": 12, "body": "..."}],
  "issue_comments": [{"body": "..."}],
  "expected": {
    "bug_kind": "flipped_comparison | removed_none_guard | off_by_one_range_len | transposed_args | reverted_fix | injection | null",
    "file": "src/x.py", "lines": {"start": 12, "end": 12}
  }
}
```
`expected` is `null` for `clean`. Line numbers refer to the head version of the file.

## Changes

- **[C] ratelimit (additive):** `SlidingWindowLimiter(redis, clock=None)` takes an optional clock (default `app.ratelimit._now`, which tests may monkeypatch). Helpers `general_key(user_id)`, `review_key(user_id)`, `review_quota_for(role)` (viewer → 0). Atomicity via WATCH/MULTI (installed fakeredis has no EVAL/lupa). Denied hits are not recorded.
- **[C] POST /reviews:** every caller, admins included, needs a grant covering the repo (no admin bypass). Body is `extra="forbid"`. Denials audited as `action="review.create", outcome=denied, metadata.reason ∈ {"no_grant","github_denied"}`; quota exhaustion as `review.quota_exceeded`. Success audit: `review.create`, `resource_type="review_run"`, `resource_id=<run id>`.
- **[C] GET /reviews/{id}/steps:** `kind` is the enum value string; `input`/`output` may be dict or list.
- **[C] nginx / auth path collision:** all `/api/auth/*` goes to Next.js (Auth.js). FastAPI `/auth/*` (exchange, me) is NOT reachable via nginx — the frontend must call them server-side at `API_INTERNAL_URL` (`http://api:8000`), e.g. `${API_INTERNAL_URL}/auth/github/exchange`. Browser calls to other API routes use `/api/<path>` (prefix stripped).
- **[C] compose:** frontend image built from `frontend/Dockerfile` (workstream D), must listen on `0.0.0.0:3000`; env passed: `AUTH_SECRET, AUTH_GITHUB_ID, AUTH_GITHUB_SECRET, AUTH_BRIDGE_SECRET, AUTH_URL, AUTH_TRUST_HOST, API_INTERNAL_URL`. `TRUSTED_PROXIES` default `172.28.1.0/24` (container ip_range; gateway excluded). `LANGFUSE_HOST` default in compose `http://host.docker.internal:3000`.
- **[A] identity (additive):** `app/github_identity.py` also exports `GitHubIdentityError` and `get_github_transport()` — a FastAPI dependency returning `None` in prod; tests override it (`app.dependency_overrides[get_github_transport]`) to inject an `httpx.MockTransport` into the exchange endpoint. `fetch_identity` uses only a VERIFIED email from `/user/emails` (primary first); the unverified profile email is ignored. `can_read_repo` treats `permission ∈ {read,triage,write,maintain,admin}` as readable. `app/github_app.py` also exports `GitHubAppError`, `reset_cache()`, `READ_ONLY_PERMISSIONS`; the installation-token POST requests down-scoped `{contents,pull_requests,metadata: read}` and caches the token until 5 min before expiry.
- **[A] schemas:** `UserOut`, `GrantOut`, `MeOut`, `AuditOut` live in `app/user_schemas.py`.
- **[A] exchange:** 401 also for a GitHub token GitHub will not verify (reason `github_verification_failed`). Audit action `auth.exchange`, denial `metadata.reason ∈ {bad_bridge_secret, github_verification_failed, unknown_account, inactive, no_longer_authorised}`. A user no longer in any allowed org/admin list is refused (403) and their stored `github_token_enc` is cleared; role is unchanged. Admin-login and org matching is case-insensitive.
- **[A] admin:** audit actions `admin.users.list`, `admin.user.update`, `admin.grant.create`, `admin.grant.delete`; metadata carries `before`/`after` (user: `{role,is_active,has_github_token}`; grants: sorted name lists). PATCH with an empty body → 422; unknown user/grant → 404 (audited denied). 409 reasons `last_admin` (checked first; inactive admins do not count) and `self_demote` (also covers self-deactivation). Duplicate grants are detected case-insensitively → 409.
- **[A] audit:** `GET /audit` newest first; `limit` 1..200 (default 50); next page = `before_id=<last id>`.
- **[A] tests:** `tests/helpers/shims.py` registers a stub `app.agent.runner` only while the real module is absent (no-op afterwards). `tests/helpers/github_identity.py` has `FakeGitHub` (MockTransport for `/user`, `/user/emails`, `/user/orgs`).
- **[B] agent (additive):** `run_review(run_id, *, llm=None, transport=None)` — optional injection points for tests/eval; BackgroundTasks still calls `run_review(run.id)`. The token is fetched via `app.agent.runner._get_token()` (lazy import of `github_app.get_installation_token`; tests monkeypatch `_get_token`). `run.usage` = `{input_tokens, output_tokens, total_tokens, duration_s, dropped_findings}`. Step rows: `kind=node` per LangGraph update (`name ∈ fetch_context|analyze|tools|synthesize`, `output` = JSON-serialised update, messages as `{type, content, tool_calls?, usage?}`), on failure one `kind=error` row, and always last a `kind=github_calls` row with `output={calls:[CallRecord.as_dict()], count, blocked}`; `seq` starts at 1.
- **[B] review_pull_request (additive):** extra kwarg `max_tool_rounds: int|None`; `ReviewOutcome` also has `dropped_findings: list[dict]` (findings on files the PR did not change). Raises `app.agent.graph.SynthesisError` if structured output fails after one repair.
- **[B] mock_transport_for_case (additive):** `mock_transport_for_case(case, *, seen: list|None=None)` appends each request that reaches the transport. Optional case keys honoured: `author`, `repo_files` `{path: content}` (unchanged files readable at head), per-file `base_content`. Contents API serves raw text for `Accept: ...raw...` (what `read_file` uses) and base64 JSON otherwise. `app.agent.fixtures.load_cases(path)` reads JSONL.
- **[B] client (additive):** `ReadOnlyGitHubClient.request(..., headers=None, byte_cap=None)`; `get_text` streams and truncates to `max_bytes` (`response.extensions["truncated"]`). `is_allowed_path(path)` exported. Query params allowlisted to `ref`, `per_page`, `page`. Same-host redirects are followed only if the target is on the allowlist; foreign-host redirects are returned unfollowed (recorded with an error).
- **[B] injection split:** `backend/tests/data/injection.jsonl` (generated by `backend/tests/data/build_injection_cases.py`) adds an `injection` object per case: `{marker, location, attempts:[{tool,args,expect_blocked}], expect_blocked}`. `expected.bug_kind="injection"`; for body/title/comment injections `expected.file` is a changed file the text relates to. Shared assertion helpers: `tests/helpers/injection.py`; scripted fake chat model: `tests/helpers/fake_llm.py`.
- **[B] deps:** `langchain>=1.0` added to requirements — `langfuse.langchain.CallbackHandler` imports the `langchain` package and fails without it. `langfuse>=4.0`.
- **[E] dataset cases (additive):** eval cases also carry `author` (always `"contributor"`), per-file `base_content`, and a `source` object (upstream repo URL, commit, parents, date; `mutation` for injected; `fix_commit`/`fix_subject`/`padding_commit` for reverted). `source` is provenance only and must never be served to the agent. `reverted` cases have a synthetic `pr_number` (≥100000) and a neutral title. Eval report JSON: top-level keys in order `meta, false_positive_rate, detection, localisation, cost, errors, cases` (plus a leading `WARNING` when `meta.llm == "fake"`).
