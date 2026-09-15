# PR Review Agent

Point an agent at a GitHub pull request and get a structured review back: findings anchored to
specific files and line ranges, an empty review when nothing is wrong, and a visible record of
everything the agent read. The agent has **read-only** GitHub access, enforced at the transport.
Every run is stored in Postgres and traced to a self-hosted Langfuse.

The goal is not "generate review comments". It is to be **trusted**. The eval harness in
[`eval/`](eval/) exists to measure whether it is.

## Architecture

```
browser ──► nginx :80 ──┬──► /            Next.js 15 (App Router, Auth.js v5)
                        ├──► /api/auth/*  Next.js (Auth.js routes; see "Path collision")
                        └──► /api/*       FastAPI (prefix stripped) ──► LangGraph agent ──► GitHub REST (GET/HEAD only)
                                             │  │
                                             │  └──► Langfuse (self-hosted, separate compose project)
                                             ├──► Postgres 16 (identity, RBAC, audit, runs, steps)
                                             └──► Redis 7 (per-user rate limit logs)
```

- **nginx is the only service in this project that publishes a port** (`80:80`). api, frontend,
  postgres and redis are `expose`-only on an internal bridge network (`172.28.0.0/16`).
- **Sign-in.** Auth.js runs the GitHub OAuth flow. In its `jwt` callback the Next.js *server*
  calls `POST http://api:8000/auth/github/exchange` with the bridge secret and the GitHub access
  token. FastAPI re-reads the identity from GitHub itself, applies the fail-closed sign-in
  policy, and returns a backend JWT (8h), which the frontend exposes as `session.apiToken`.
  FastAPI never tries to verify Auth.js session tokens.
- **Reviews.** `POST /api/reviews` checks permission → repo grant → GitHub's own answer to "can
  this user read this repo" → the per-user hourly quota. It then commits the run and its audit
  row together and runs the agent as a background task:
  `fetch_context → analyze ⇄ tools → synthesize`. Each node is saved as an `agent_steps` row.
  The GitHub call log is always saved too, whatever the outcome.

### Path collision: `/api/auth/*`

Auth.js owns `/api/auth/*`. FastAPI also has `/auth/*` routes, which would appear at `/api/auth/*`
once nginx strips the prefix. **nginx sends all of `/api/auth/*` to Next.js, and FastAPI's
`/auth/*` routes cannot be reached through nginx at all.** The token exchange is server-to-server
by design (the Next.js server calls `API_INTERNAL_URL` directly), so leaving it off the public
edge takes away attack surface and costs nothing. `GET /auth/me` is called the same way by the
Next.js server. The reasoning is also written up in [`infra/nginx/nginx.conf`](infra/nginx/nginx.conf).

## Quick start

Prerequisites: Docker with Compose v2, a GitHub OAuth App, a GitHub App (see below), and an
Anthropic API key.

```sh
cp .env.example .env
# Fill in .env. Generate the secrets:
python -c "import secrets; print(secrets.token_urlsafe(48))"                                  # JWT_SECRET, AUTH_BRIDGE_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"     # TOKEN_ENCRYPTION_KEY (must differ from JWT_SECRET)
npx auth secret                                                                                # AUTH_SECRET
# Set GITHUB_ADMIN_IDS to your numeric GitHub user id (curl https://api.github.com/users/<login> | jq .id)
# so your first sign-in becomes admin. Ids, not logins: renamed logins can be re-registered.

./infra/langfuse.sh          # optional: tracing (see below)
docker compose up --build    # → http://localhost
```

The api container runs `alembic upgrade head` before it starts uvicorn. Compose refuses to start
if `POSTGRES_PASSWORD`, `AUTH_SECRET` or `AUTH_BRIDGE_SECRET` are unset.

An admin gives users access to repositories through the API (there is no admin UI):

```sh
curl -X POST http://localhost/api/admin/users/2/grants -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -d '{"repo_full_name": "acme/*"}'
```

Every user, admins included, needs a grant that covers a repository before they can start a
review of it.

Backend tests (no network, SQLite + fakeredis):

```sh
cd backend && .venv/Scripts/python.exe -m pytest -q      # POSIX: .venv/bin/python
```

## GitHub setup

### OAuth App (sign-in)

- Homepage URL: `http://localhost`
- Authorization callback URL: `http://localhost/api/auth/callback/github`
- Scopes requested by Auth.js: **`read:user user:email read:org`**. **Never `repo`**: that scope
  grants read-write access to every repository the user can reach.
- Put the client id and secret in `AUTH_GITHUB_ID` / `AUTH_GITHUB_SECRET`.

The backend stores the user's OAuth token encrypted with Fernet (`TOKEN_ENCRYPTION_KEY`, which
is separate from `JWT_SECRET`). It clears the token when a user is deactivated.

### GitHub App (the agent's credential)

- Repository permissions: **Contents: Read, Pull requests: Read, Metadata: Read**. Nothing else.
  No webhooks.
- Install the App on the repositories or organisations you want reviewed.
- Set `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, and `GITHUB_APP_PRIVATE_KEY` (the PEM on one
  line with literal `\n` separators, single-quoted in `.env`).
- For local development without an App, `GITHUB_READONLY_TOKEN` accepts a fine-grained read-only
  token. The transport allowlist applies either way.

## Security claims

Every claim here is backed by a test. A claim without a test would just be an assertion. All
paths are relative to `backend/tests/`.

| Claim | Test |
|---|---|
| The agent's GitHub client refuses every verb except GET/HEAD, and refuses any path not on an explicit allowlist, **before a socket opens**. Blocked attempts are still logged. Query-string smuggling and off-allowlist redirects are blocked. Raw fetches are cut off at a byte cap. | [`test_readonly.py`](backend/tests/test_readonly.py) |
| Routes ask for a `Permission`, never a role name. The role → permission matrix is enforced, and denials return 403 and are audited. | [`test_rbac.py`](backend/tests/test_rbac.py) |
| The exchange endpoint needs the bridge secret, accepts **only** an access token (extra profile fields are rejected), and re-reads identity from GitHub. Unknown GitHub accounts are rejected, not provisioned (fails closed). Role is derived at first sign-in only. The last admin cannot be demoted (409) and no one can remove their own admin role. Deactivation clears the stored GitHub token. The token is stored encrypted. | [`test_auth.py`](backend/tests/test_auth.py) |
| App-layer limits are keyed on **user id, not IP**: 600 requests/min overall, plus a review quota of 20/h for reviewers and 100/h for admins. They use a sliding-window log, so the budget does not double at a window boundary. The 21st review in an hour returns 429 with a correct `Retry-After` and is audited as `review.quota_exceeded`. Denied, forbidden and invalid requests do not consume quota. | [`test_ratelimit.py`](backend/tests/test_ratelimit.py) |
| Reviews: a repo grant is required. On top of that, GitHub is asked whether *this* user can read *this* repo, because an org grant alone is not enough. Both denials are audited. The run and its `review.create` audit row commit in one transaction. Runs the caller cannot see return 404. Repo names like `../etc`, `acme/..` are rejected. | [`test_reviews.py`](backend/tests/test_reviews.py) |
| `X-Forwarded-For` is only believed when the TCP peer is in `TRUSTED_PROXIES` (CIDR blocks supported). A client-supplied header cannot choose the IP that gets logged. | [`test_proxy_trust.py`](backend/tests/test_proxy_trust.py) |
| Audit rows are written in the same transaction as the action they describe. The helper never commits. Denials are audited as well as successes. `actor_email` is denormalised so the log survives user deletion. | [`test_audit.py`](backend/tests/test_audit.py) |
| Prompt injection in diffs, comments or file contents (≥15 cases) is reported as a `high` finding, and the GitHub call log shows no blocked call attempts. | [`test_injection.py`](backend/tests/test_injection.py) |

The nginx behaviour was checked by hand against echo upstreams, not in CI: `/api/` prefix
stripping, `/api/auth/*` → Next.js, 429 once the auth burst is used up. Validate the syntax with
`docker run --rm -v "$PWD/infra/nginx/nginx.conf:/etc/nginx/nginx.conf:ro" nginx:stable nginx -t`.

### Two rate-limit layers

- **nginx, per IP:** 30 r/s general and 10 r/m on `/api/auth/`, with `limit_req_status 429`.
  This protects the processes from floods. The `limit_req_zone` state belongs to each nginx
  instance, so two nginx containers give a client two independent budgets. On Docker Desktop,
  published-port traffic can arrive from a single gateway address, which puts every client in
  one bucket.
- **API, per user, in Redis:** this is the cost control. LLM spend and the GitHub App's quota are
  used up per *review*. One user could burn a day's budget from a single IP at two requests a
  minute without ever tripping nginx. The Redis limit is shared across API replicas.

### Proxy trust under compose

The `app` network is `172.28.0.0/16`, and containers get addresses from
`ip_range: 172.28.1.0/24`. `TRUSTED_PROXIES` defaults to that `/24`, which **excludes the
gateway `172.28.0.1`**. Published-port traffic may appear to come from the gateway, and trusting
it would let any client write its own `X-Forwarded-For`. uvicorn runs with `--no-proxy-headers`
so that only the app decides whom to trust (`app/netutil.py`).

## Tracing with Langfuse

```sh
./infra/langfuse.sh              # downloads the official docker-compose.yml from langfuse/langfuse into infra/langfuse/ and starts it as project "langfuse"
LANGFUSE_REF=v3.100.0 ./infra/langfuse.sh pull   # pin a tag
./infra/langfuse.sh down
```

Open `http://localhost:3000`, create a project, put its keys in `LANGFUSE_PUBLIC_KEY` /
`LANGFUSE_SECRET_KEY`, then run `docker compose up -d api`. Replace every `CHANGEME` value in
`infra/langfuse/docker-compose.yml` before running it anywhere but your own machine.

**How the api reaches it:** through the host. Langfuse's own compose publishes `langfuse-web` on
host port 3000. The api uses `LANGFUSE_HOST=http://host.docker.internal:3000`, and
`extra_hosts: host.docker.internal:host-gateway` makes that name resolve on Linux as well. The
two stacks share no network, so this app starts with or without Langfuse, and Langfuse publishes
no ports beyond what its upstream compose already does.

**The tension:** "only nginx publishes a port" holds for this project's compose. Langfuse is a
separate operator tool, and its compose publishes `:3000` (its minio, clickhouse, postgres and
redis ports are bound to 127.0.0.1). If that is unacceptable, remove its `ports:`, attach
`langfuse-web` to a shared external network, and set `LANGFUSE_HOST=http://langfuse-web:3000`.
That route makes the network something that must exist before `docker compose up`.

`agent_steps` in Postgres duplicates the trace on purpose. Langfuse has its own retention and is
for debugging. The table is the record you keep.

## Eval

See [`eval/`](eval/). The dataset has three JSONL splits (`injected`, `reverted`, `clean`). Each
case carries its own patches, so eval runs never hit GitHub. Metrics are reported together and
in this order: **false-positive rate on `clean`**, detection rate by split and by bug kind,
localisation (file match + line overlap within 5 lines), and cost. `--baseline` scores "flag
every changed file as medium"; if the agent does not clearly beat it, the LLM is not earning its
cost.

```sh
python -m eval.run_eval --dataset eval/data/v1.jsonl --report eval/reports/v1.json
```

Quote `reverted` as the quality number. `injected` is a regression harness for prompt changes,
because injected bugs are more uniform than real ones.

## Known gaps

Deliberately out of scope:

- **No job queue.** Reviews run in FastAPI `BackgroundTasks`, which is fine up to about 10
  concurrent reviews. A crash while a run is in progress leaves it in `running`.
- **No webhook triggers.** Reviews are started by users so the audit trail stays attributable.
- **No admin UI.** The `/admin` API is the interface.
- **No TLS.** The 443 server block and the HSTS header are in `infra/nginx/nginx.conf`,
  commented out. Sending HSTS over plain HTTP would pin `localhost` to HTTPS in your browser.
- **No token refresh.** Sessions expire after 8h and the user signs in again.
- **No repo-wide symbol indexing.** It is the right next step, but only once the eval gives a
  number to improve against.
