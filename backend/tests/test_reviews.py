"""POST/GET /reviews: grant check, GitHub read check, visibility, audit-with-action commits."""

import types
import uuid

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models import (
    AgentStep,
    AuditLog,
    Outcome,
    RepoGrant,
    ReviewRun,
    Role,
    RunStatus,
    StepKind,
)


@pytest.fixture
def env(monkeypatch):
    from app.routers import reviews

    ns = types.SimpleNamespace(queued=[], github_calls=[], github_allows=True)

    async def fake_can_read_repo(*, user_login, repo_full_name, transport=None):
        ns.github_calls.append((user_login, repo_full_name))
        return ns.github_allows

    async def fake_run_review(run_id):
        ns.queued.append(run_id)

    monkeypatch.setattr(reviews, "can_read_repo", fake_can_read_repo)
    monkeypatch.setattr(reviews, "run_review", fake_run_review)
    return ns


async def _grant(session, user, repo):
    session.add(RepoGrant(user_id=user.id, repo_full_name=repo))
    await session.commit()


async def _run(session, user, repo, pr=1) -> ReviewRun:
    run = ReviewRun(
        user_id=user.id if user else None, repo_full_name=repo, pr_number=pr,
        status=RunStatus.succeeded, model="m", result={"summary": "ok", "findings": []},
    )
    session.add(run)
    await session.commit()
    return run


async def _audits(session, action=None):
    stmt = select(AuditLog).order_by(AuditLog.id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return (await session.scalars(stmt)).all()


async def _count_runs(session) -> int:
    return await session.scalar(select(func.count()).select_from(ReviewRun))


# --- POST /reviews ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"repo": "../etc", "pr_number": 1},
        {"repo": "acme/..", "pr_number": 1},
        {"repo": "acme/.", "pr_number": 1},
        {"repo": "acme/*", "pr_number": 1},
        {"repo": "-acme/x", "pr_number": 1},
        {"repo": "acme/x/y", "pr_number": 1},
        {"repo": "acme/x", "pr_number": 0},
        {"repo": "acme/x", "pr_number": -3},
        {"repo": "acme/x", "pr_number": 1, "user": {"role": "admin"}},
    ],
)
async def test_create_rejects_invalid_body(client, session, make_user, auth_headers, env, body):
    user = await make_user(Role.reviewer)
    await _grant(session, user, "acme/*")
    r = await client.post("/reviews", json=body, headers=auth_headers(user))
    assert r.status_code == 422
    assert await _count_runs(session) == 0
    assert env.github_calls == [] and env.queued == []


async def test_dot_github_repo_is_valid(client, session, make_user, auth_headers, env):
    user = await make_user(Role.reviewer)
    await _grant(session, user, "acme/.github")
    r = await client.post("/reviews", json={"repo": "acme/.github", "pr_number": 5},
                          headers=auth_headers(user))
    assert r.status_code == 202


async def test_create_without_grant_is_403_and_audited(
    client, session, make_user, auth_headers, env
):
    user = await make_user(Role.reviewer)
    await _grant(session, user, "acme/other")
    r = await client.post("/reviews", json={"repo": "acme/widgets", "pr_number": 7},
                          headers=auth_headers(user))
    assert r.status_code == 403
    assert await _count_runs(session) == 0
    assert env.github_calls == []  # grant is checked before asking GitHub
    (row,) = await _audits(session, "review.create")
    assert row.outcome == Outcome.denied
    assert row.actor_user_id == user.id
    assert row.metadata_["reason"] == "no_grant"
    assert row.metadata_["repo"] == "acme/widgets"


async def test_org_grant_is_not_enough_when_github_denies(
    client, session, make_user, auth_headers, env
):
    user = await make_user(Role.reviewer, login="octo")
    await _grant(session, user, "acme/*")
    env.github_allows = False
    r = await client.post("/reviews", json={"repo": "ACME/secret", "pr_number": 3},
                          headers=auth_headers(user))
    assert r.status_code == 403
    assert env.github_calls == [("octo", "ACME/secret")]
    assert await _count_runs(session) == 0
    assert env.queued == []
    (row,) = await _audits(session, "review.create")
    assert (row.outcome, row.metadata_["reason"]) == (Outcome.denied, "github_denied")


async def test_admin_bypasses_local_grants_but_github_is_still_asked(
    client, session, make_user, auth_headers, env
):
    admin = await make_user(Role.admin, login="root")  # no repo_grants at all
    r = await client.post("/reviews", json={"repo": "acme/widgets", "pr_number": 4},
                          headers=auth_headers(admin))
    assert r.status_code == 202, r.text
    assert env.github_calls == [("root", "acme/widgets")]


async def test_admin_is_refused_a_repo_github_says_they_cannot_read(
    client, session, make_user, auth_headers, env
):
    admin = await make_user(Role.admin, login="root")
    await _grant(session, admin, "acme/*")  # even an explicit local grant does not override GitHub
    env.github_allows = False
    r = await client.post("/reviews", json={"repo": "acme/secret", "pr_number": 9},
                          headers=auth_headers(admin))
    assert r.status_code == 403
    assert env.github_calls == [("root", "acme/secret")]
    assert await _count_runs(session) == 0 and env.queued == []
    (row,) = await _audits(session, "review.create")
    assert (row.outcome, row.metadata_["reason"]) == (Outcome.denied, "github_denied")
    assert row.actor_user_id == admin.id


async def test_reviewer_does_not_get_the_admin_grant_bypass(
    client, session, make_user, auth_headers, env
):
    reviewer = await make_user(Role.reviewer)
    r = await client.post("/reviews", json={"repo": "acme/widgets", "pr_number": 4},
                          headers=auth_headers(reviewer))
    assert r.status_code == 403 and env.github_calls == []


async def test_viewer_cannot_create(client, session, make_user, auth_headers, env):
    viewer = await make_user(Role.viewer)
    await _grant(session, viewer, "acme/*")
    r = await client.post("/reviews", json={"repo": "acme/x", "pr_number": 1},
                          headers=auth_headers(viewer))
    assert r.status_code == 403
    assert env.github_calls == []
    (row,) = await _audits(session, "authz.denied")
    assert row.metadata_["permission"] == "review:create"


async def test_create_requires_auth(client, env):
    r = await client.post("/reviews", json={"repo": "acme/x", "pr_number": 1})
    assert r.status_code == 401


async def test_create_success_commits_run_and_audit_and_queues(
    client, session, make_user, auth_headers, env
):
    user = await make_user(Role.reviewer)
    await _grant(session, user, "Acme/Widgets")  # case-insensitive
    r = await client.post("/reviews", json={"repo": "acme/widgets", "pr_number": 42},
                          headers={**auth_headers(user), "X-Request-ID": "req-123"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["repo"] == "acme/widgets" and body["pr_number"] == 42
    assert body["model"] == get_settings().llm_model
    assert body["user_id"] == user.id
    assert set(body) >= {"id", "langfuse_trace_id", "result", "usage", "error", "created_at",
                         "started_at", "finished_at"}

    run_id = uuid.UUID(body["id"])
    run = await session.get(ReviewRun, run_id)
    assert run is not None and run.status == RunStatus.queued
    assert env.queued == [run_id]

    (row,) = await _audits(session, "review.create")
    assert row.outcome == Outcome.success
    assert (row.resource_type, row.resource_id) == ("review_run", str(run_id))
    assert row.request_id == "req-123"
    assert row.actor_ip == "203.0.113.7"


async def test_run_and_audit_share_one_transaction(
    client, session, make_user, auth_headers, env, monkeypatch
):
    """If the audit row cannot be written, the run must not exist either."""
    from app.models import AuditLog as _AuditLog
    from app.routers import reviews

    def broken_audit(session, **kw):
        row = _AuditLog(action=None, outcome=Outcome.success)  # NOT NULL violation at flush
        session.add(row)
        return row

    monkeypatch.setattr(reviews, "record_audit", broken_audit)
    user = await make_user(Role.reviewer)
    await _grant(session, user, "acme/*")
    with pytest.raises(Exception):
        await client.post("/reviews", json={"repo": "acme/x", "pr_number": 1},
                          headers=auth_headers(user))
    assert await _count_runs(session) == 0
    assert env.queued == []


# --- visibility -------------------------------------------------------------------------


async def test_visibility_rules(client, session, make_user, auth_headers, env):
    alice = await make_user(Role.reviewer)
    bob = await make_user(Role.reviewer)
    viewer = await make_user(Role.viewer)
    admin = await make_user(Role.admin)
    await _grant(session, alice, "acme/widgets")
    await _grant(session, viewer, "ACME/*")
    await _grant(session, bob, "acme/widgets-extra")  # not a prefix match

    widgets = await _run(session, alice, "acme/widgets")
    other_org = await _run(session, admin, "globex/core")
    bob_own = await _run(session, bob, "initech/tps")

    async def ids(user):
        r = await client.get("/reviews", headers=auth_headers(user))
        assert r.status_code == 200
        return {x["id"] for x in r.json()}

    assert await ids(alice) == {str(widgets.id)}
    assert await ids(bob) == {str(bob_own.id)}
    assert await ids(viewer) == {str(widgets.id)}
    assert await ids(admin) == {str(widgets.id), str(other_org.id), str(bob_own.id)}

    # Detail and steps: invisible runs are 404, not 403.
    for path in (f"/reviews/{widgets.id}", f"/reviews/{widgets.id}/steps"):
        assert (await client.get(path, headers=auth_headers(bob))).status_code == 404
        assert (await client.get(path, headers=auth_headers(viewer))).status_code == 200
        assert (await client.get(path, headers=auth_headers(admin))).status_code == 200
    assert (await client.get(f"/reviews/{other_org.id}", headers=auth_headers(alice))).status_code == 404
    assert (await client.get(f"/reviews/{bob_own.id}", headers=auth_headers(bob))).status_code == 200

    r = await client.get(f"/reviews/{uuid.uuid4()}", headers=auth_headers(admin))
    assert r.status_code == 404
    r = await client.get("/reviews/not-a-uuid", headers=auth_headers(admin))
    assert r.status_code == 422


async def test_wildcard_grant_does_not_match_owner_prefix(client, session, make_user, auth_headers):
    viewer = await make_user(Role.viewer)
    owner = await make_user(Role.admin)
    await _grant(session, viewer, "acm/*")
    run = await _run(session, owner, "acme/widgets")
    r = await client.get("/reviews", headers=auth_headers(viewer))
    assert r.json() == []
    assert (await client.get(f"/reviews/{run.id}", headers=auth_headers(viewer))).status_code == 404


async def test_detail_and_steps_payload(client, session, make_user, auth_headers):
    user = await make_user(Role.reviewer)
    run = await _run(session, user, "acme/widgets")
    session.add_all([
        AgentStep(run_id=run.id, seq=2, kind=StepKind.github_calls, name="github_calls",
                  output={"calls": [{"method": "GET", "path": "/repos/acme/widgets"}]}),
        AgentStep(run_id=run.id, seq=1, kind=StepKind.node, name="fetch_context",
                  input={"repo": "acme/widgets"}, output={"files": 2}, latency_ms=12),
    ])
    await session.commit()

    r = await client.get(f"/reviews/{run.id}", headers=auth_headers(user))
    assert r.status_code == 200
    assert r.json()["result"] == {"summary": "ok", "findings": []}

    r = await client.get(f"/reviews/{run.id}/steps", headers=auth_headers(user))
    steps = r.json()
    assert [s["seq"] for s in steps] == [1, 2]
    assert steps[0] == {**steps[0], "kind": "node", "name": "fetch_context", "latency_ms": 12,
                        "input": {"repo": "acme/widgets"}, "output": {"files": 2}}
    assert steps[1]["kind"] == "github_calls"
    assert set(steps[0]) == {"seq", "kind", "name", "input", "output", "latency_ms", "created_at"}


async def test_list_limit(client, session, make_user, auth_headers):
    user = await make_user(Role.reviewer)
    for i in range(5):
        await _run(session, user, "acme/widgets", pr=i + 1)
    r = await client.get("/reviews?limit=2", headers=auth_headers(user))
    assert len(r.json()) == 2
    assert (await client.get("/reviews?limit=0", headers=auth_headers(user))).status_code == 422
