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
- **Reviews.** `POST /api/reviews` checks permission → repo grant (skipped for holders of
  `grant:manage`) → a non-consuming quota check → GitHub's own answer to "can this user read this
  repo" (asked for every role, admins included) → consumes one unit of the per-user hourly quota. It then commits the run and its audit
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

Prerequisites: Docker with Compose v2, a GitHub OAuth App, a GitHub App (see below), and an LLM
key. By default that's an OpenRouter key: set `LLM_API_KEY` and `AGENT_MODEL`, e.g.
`anthropic/<model>`. Setting `LLM_PROVIDER=anthropic` uses an Anthropic key directly. Both are
built in one place, `backend/app/agent/llm.py`.

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
| App-layer limits are keyed on **user id, not IP**: 600 requests/min overall, plus a review quota of 20/h for reviewers and 100/h for admins. They use a sliding-window log, so the budget does not double at a window boundary. The 21st review in an hour returns 429 with a correct `Retry-After` and is audited as `review.quota_exceeded`. Denied, forbidden and invalid requests do not consume quota. An over-quota request is refused before GitHub is called, so it cannot spend the GitHub App's API budget. | [`test_ratelimit.py`](backend/tests/test_ratelimit.py) |
| Reviews: a repo grant is required, except for holders of `grant:manage` (admins), who could grant themselves anyway. GitHub is then asked whether *this* user can read *this* repo for **every role, admins included**: an org grant alone is not enough, and admin is not a licence to read repos the person cannot see. Both denials are audited. The run and its `review.create` audit row commit in one transaction. Runs the caller cannot see return 404. Repo names like `../etc`, `acme/..` are rejected. | [`test_reviews.py`](backend/tests/test_reviews.py) |
| `X-Forwarded-For` is only believed when the TCP peer is in `TRUSTED_PROXIES` (CIDR blocks supported). A client-supplied header cannot choose the IP that gets logged. | [`test_proxy_trust.py`](backend/tests/test_proxy_trust.py) |
| Audit rows are written in the same transaction as the action they describe. The helper never commits. Denials are audited as well as successes. `actor_email` is denormalised so the log survives user deletion. | [`test_audit.py`](backend/tests/test_audit.py) |
| Prompt injection in diffs, comments or file contents (20 cases): the GitHub call log shows no blocked call attempts, and the agent reports the injection as a `high` finding. **Only the first half is verified today — see below.** | [`test_injection.py`](backend/tests/test_injection.py) |

**The injection claim is only half-verified.**
- **Offline tests cover the transport guard only.** A scripted fake model obeys each injection. The
  tests prove the read-only client blocks what the injection asks for, and that the harness
  detects it. They also check injected text reaches the model only inside the untrusted-data
  delimiters.
- **"Reports injections as `high` findings" is unverified until the live run**
  (`pytest tests/test_injection.py --run-live` with `LLM_API_KEY` and `AGENT_MODEL` set). That is a statement
  about the real model's behaviour, and no offline test can back it. Those tests skip without a key.

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

> **Check `docker compose ls` first if this machine already runs a Langfuse.** `infra/langfuse.sh`
> assumes it owns the containers in its compose project (`pr-review-agent-langfuse`). `up` would
> recreate any containers already in that project with this repo's override and the upstream
> image tags, which can migrate their data. The script refuses to take over a project it did
> not start. But an existing Langfuse under any project name still shares the host's Docker
> daemon, image cache and disk, so look before you run it.

```sh
docker compose up -d             # the app first: it creates the `pr-review-agent_app` network
./infra/langfuse.sh              # downloads the official docker-compose.yml into infra/langfuse/, starts project "pr-review-agent-langfuse"
LANGFUSE_REF=v4.0.0 ./infra/langfuse.sh pull   # pin a tag
./infra/langfuse.sh down
```

Open `http://langfuse.localhost`, create a project, put its keys in `LANGFUSE_PUBLIC_KEY` /
`LANGFUSE_SECRET_KEY`, then run `docker compose up -d api`. Replace every `CHANGEME` value in
`infra/langfuse/docker-compose.yml`, and set `AUTH_DISABLE_SIGNUP=true` once your account exists,
before running it anywhere but your own machine.

**Nothing but nginx publishes a port, Langfuse included.** The upstream compose file is used
unmodified. `infra/langfuse.override.yml` is layered on top and does two things:
- It resets every upstream port mapping: 3000, 3030 and 9090, plus loopback-only 5432, 6379, 8123, 9000 and 9091.
- It attaches only `langfuse-web` to this app's `app` network, pinned to `172.28.2.10`.
  - That address is inside the subnet but outside the `172.28.1.0/24` range `TRUSTED_PROXIES` covers,
    so Langfuse can never act as a trusted proxy and choose the IP in the audit log.
  - Its datastores stay on Langfuse's private network.

The api traces to `LANGFUSE_HOST=http://langfuse-web:3000`, and nginx serves the UI by host name at
`http://langfuse.localhost`. Browsers resolve `*.localhost` to loopback, so there is no second port
and no clash with Next.js dev on :3000. Until Langfuse is started, that host returns 502 and the api
silently skips tracing.

Known gap: media attachments in traces use MinIO presigned URLs, which browsers can no longer reach
without a published MinIO port. This agent sends text only.

`agent_steps` in Postgres duplicates the trace on purpose. Langfuse has its own retention and is
for debugging. The table is the record you keep.

## Eval

See [`eval/`](eval/). The dataset has three JSONL splits (`injected`, `reverted`, `clean`). Each
case carries its own patches, so eval runs never hit GitHub. Metrics are reported together and
in this order:
1. **False-positive rate on `clean`.**
2. **Detection rate** by split and by bug kind: right file, overlapping the bug within 5 lines.
3. **Localisation rate**, as a share of detected bugs: the detecting finding's range is also narrow.
4. **Cost.**

`--baseline` flags every changed file, whole file, as medium. It scores 100% detection, 100% FP
and 0% localisation (`eval/reports/baseline.json`). The agent has to beat it on FP rate and
localisation; if it doesn't, the LLM is not earning its cost.

```sh
python -m eval.run_eval --dataset eval/data/v1.jsonl --report eval/reports/v1.json   # full run
python -m eval.dev_split --verify && python -m eval.run_eval --dataset eval/data/dev.jsonl --report eval/reports/dev.json
```

Every report opens with a `parse_errors` block. If synthesis output fails to parse often, every
other number is noise.

**Two agent reports compare only if `dataset_sha256`, `meta.scoring`, `meta.provider`,
`meta.model` and `meta.temperature` all match.** Temperature defaults to 0. So a Haiku run is never silently compared with a Sonnet run, and a
60-case dev run never with the full run. The dev subset's 35 clean cases give roughly ±8% slop on
the FP rate: good for "did this change help", not for a headline number.

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
