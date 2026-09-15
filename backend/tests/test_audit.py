"""Audit helper transaction semantics, denial auditing, before/after metadata, and GET /audit."""

from sqlalchemy import func, select

import tests.helpers.shims  # noqa: F401  (must precede app imports)
from app.audit import record_audit
from app.models import AuditLog, Outcome, Role, User
from app.request_context import RequestContext
from app.security import encrypt_token

CTX = RequestContext(ip="198.51.100.1", user_agent="pytest", request_id="rid-1")


async def _count(sessionmaker) -> int:
    async with sessionmaker() as s:
        return (await s.execute(select(func.count()).select_from(AuditLog))).scalar_one()


async def _rows(sessionmaker, action=None) -> list[AuditLog]:
    async with sessionmaker() as s:
        stmt = select(AuditLog).order_by(AuditLog.id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        return list((await s.execute(stmt)).scalars().all())


async def test_helper_never_commits_rollback_discards(sessionmaker):
    async with sessionmaker() as s:
        row = record_audit(s, ctx=CTX, actor=None, action="test.action", outcome=Outcome.success)
        assert row in s.new  # pending, not flushed or committed
        await s.rollback()
    assert await _count(sessionmaker) == 0


async def test_helper_row_lands_with_callers_commit(sessionmaker, make_user):
    actor = await make_user(Role.admin, email=None, login="nomail")
    async with sessionmaker() as s:
        actor = await s.get(User, actor.id)
        record_audit(s, ctx=CTX, actor=actor, action="test.action", outcome=Outcome.denied,
                     resource_type="thing", resource_id=42, metadata={"k": "v"})
        assert await _count(sessionmaker) == 0  # invisible to others until the caller commits
        await s.commit()
    [row] = await _rows(sessionmaker)
    assert (row.actor_user_id, row.actor_email) == (actor.id, "nomail")  # falls back to login
    assert (row.actor_ip, row.user_agent, row.request_id) == ("198.51.100.1", "pytest", "rid-1")
    assert (row.resource_type, row.resource_id, row.metadata_) == ("thing", "42", {"k": "v"})


async def test_action_and_audit_share_a_transaction(sessionmaker, make_user):
    """If the action is rolled back, its audit row goes with it (and vice versa)."""
    target = await make_user(Role.viewer)
    async with sessionmaker() as s:
        u = await s.get(User, target.id)
        u.role = Role.reviewer
        record_audit(s, ctx=CTX, actor=None, action="admin.user.update", outcome=Outcome.success)
        await s.rollback()
    async with sessionmaker() as s:
        assert (await s.get(User, target.id)).role == Role.viewer
    assert await _count(sessionmaker) == 0


async def test_denial_is_audited(client, sessionmaker, make_user, auth_headers):
    viewer = await make_user(Role.viewer)
    r = await client.get("/audit", headers={**auth_headers(viewer), "User-Agent": "ua-test", "X-Request-ID": "req-123"})
    assert r.status_code == 403
    [row] = await _rows(sessionmaker, "authz.denied")
    assert row.outcome == Outcome.denied
    assert row.actor_user_id == viewer.id and row.actor_email == viewer.email
    assert row.user_agent == "ua-test" and row.request_id == "req-123"
    assert row.metadata_ == {"permission": "audit:read", "role": "viewer"}


async def test_patch_records_before_after(client, sessionmaker, make_user, auth_headers):
    admin = await make_user(Role.admin)
    target = await make_user(Role.reviewer, github_token_enc=encrypt_token("gho_t"))
    r = await client.patch(f"/admin/users/{target.id}", json={"role": "viewer", "is_active": False},
                           headers=auth_headers(admin))
    assert r.status_code == 200
    [row] = await _rows(sessionmaker, "admin.user.update")
    assert row.outcome == Outcome.success
    assert (row.actor_user_id, row.resource_type, row.resource_id) == (admin.id, "user", str(target.id))
    assert row.metadata_["before"] == {"role": "reviewer", "is_active": True, "has_github_token": True}
    assert row.metadata_["after"] == {"role": "viewer", "is_active": False, "has_github_token": False}
    assert row.metadata_["requested"] == {"role": "viewer", "is_active": False}


async def test_refused_patch_audited_and_unchanged(client, sessionmaker, make_user, auth_headers):
    admin = await make_user(Role.admin)
    r = await client.patch(f"/admin/users/{admin.id}", json={"role": "viewer"}, headers=auth_headers(admin))
    assert r.status_code == 409
    [row] = await _rows(sessionmaker, "admin.user.update")
    assert row.outcome == Outcome.denied
    assert row.metadata_["before"] == {"role": "admin", "is_active": True, "has_github_token": False}
    assert "after" not in row.metadata_


async def test_grant_create_delete_before_after(client, sessionmaker, make_user, auth_headers):
    admin = await make_user(Role.admin)
    target = await make_user(Role.reviewer)
    h = auth_headers(admin)
    url = f"/admin/users/{target.id}/grants"
    await client.post(url, json={"repo_full_name": "acme/a"}, headers=h)
    gid = (await client.post(url, json={"repo_full_name": "acme/*"}, headers=h)).json()["id"]
    assert (await client.delete(f"{url}/{gid}", headers=h)).status_code == 204

    created = await _rows(sessionmaker, "admin.grant.create")
    assert created[1].metadata_["before"] == ["acme/a"]
    assert created[1].metadata_["after"] == ["acme/*", "acme/a"]
    [deleted] = await _rows(sessionmaker, "admin.grant.delete")
    assert (deleted.resource_type, deleted.resource_id) == ("repo_grant", str(gid))
    assert deleted.metadata_["before"] == ["acme/*", "acme/a"]
    assert deleted.metadata_["after"] == ["acme/a"]


async def test_list_users_is_audited(client, sessionmaker, make_user, auth_headers):
    admin = await make_user(Role.admin)
    await make_user(Role.viewer)
    assert (await client.get("/admin/users", headers=auth_headers(admin))).status_code == 200
    [row] = await _rows(sessionmaker, "admin.users.list")
    assert row.metadata_ == {"count": 2}


async def test_audit_keyset_pagination_and_filter(client, sessionmaker, make_user, auth_headers):
    admin = await make_user(Role.admin)
    async with sessionmaker() as s:
        for i in range(5):
            record_audit(s, ctx=None, actor=None, action="a.even" if i % 2 == 0 else "a.odd",
                         outcome=Outcome.success, metadata={"i": i})
        await s.commit()
    h = auth_headers(admin)

    page1 = (await client.get("/audit", params={"limit": 2}, headers=h)).json()
    assert [r["metadata"]["i"] for r in page1] == [4, 3]
    page2 = (await client.get("/audit", params={"limit": 2, "before_id": page1[-1]["id"]}, headers=h)).json()
    assert [r["metadata"]["i"] for r in page2] == [2, 1]
    page3 = (await client.get("/audit", params={"limit": 2, "before_id": page2[-1]["id"]}, headers=h)).json()
    assert [r["metadata"]["i"] for r in page3] == [0]

    odd = (await client.get("/audit", params={"action": "a.odd"}, headers=h)).json()
    assert [r["metadata"]["i"] for r in odd] == [3, 1]
    assert set(odd[0]) == {
        "id", "created_at", "actor_user_id", "actor_email", "actor_ip", "user_agent", "action",
        "resource_type", "resource_id", "outcome", "request_id", "metadata",
    }
    for bad in ({"limit": 0}, {"limit": 1000}, {"before_id": 0}, {"before_id": "x"}):
        assert (await client.get("/audit", params=bad, headers=h)).status_code == 422
