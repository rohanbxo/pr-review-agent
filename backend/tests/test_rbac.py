"""The permission matrix, the `require()` dependency, and route-level enforcement (403s audited)."""

import pytest
from fastapi import Depends
from sqlalchemy import select

import tests.helpers.shims  # noqa: F401  (must precede app imports)
from app.deps import require
from app.models import AuditLog, Outcome, Role, User
from app.rbac import ROLE_PERMISSIONS, Permission, has_permission, permissions_for

P = Permission

# Written out explicitly: this table IS the specification the code is checked against.
EXPECTED: dict[Role, set[Permission]] = {
    Role.admin: {P.review_create, P.review_read, P.review_read_all, P.audit_read, P.user_manage, P.grant_manage},
    Role.reviewer: {P.review_create, P.review_read},
    Role.viewer: {P.review_read},
}

MATRIX = [(role, perm) for role in Role for perm in Permission]


def test_every_role_has_an_entry():
    assert set(ROLE_PERMISSIONS) == set(Role)


def test_permission_values():
    assert {p.value for p in Permission} == {
        "review:create", "review:read", "review:read_all", "audit:read", "user:manage", "grant:manage",
    }


@pytest.mark.parametrize("role,perm", MATRIX, ids=[f"{r.value}-{p.value}" for r, p in MATRIX])
def test_matrix(role, perm):
    assert has_permission(role, perm) is (perm in EXPECTED[role])


@pytest.mark.parametrize("role", list(Role))
def test_permissions_for(role):
    assert permissions_for(role) == sorted(p.value for p in EXPECTED[role])


def test_unknown_role_has_nothing():
    assert has_permission("superuser", P.review_read) is False  # type: ignore[arg-type]
    assert permissions_for("superuser") == []  # type: ignore[arg-type]


# --- require() for every (role, permission) through a real request --------------------------

@pytest.fixture
def probe_app(app):
    for perm in Permission:
        async def _probe(user: User = Depends(require(perm))) -> dict:
            return {"ok": True}

        app.add_api_route(f"/_probe/{perm.name}", _probe, methods=["GET"])
    return app


async def _audit(session, action=None):
    session.expire_all()
    stmt = select(AuditLog).order_by(AuditLog.id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return (await session.execute(stmt)).scalars().all()


@pytest.mark.parametrize("role,perm", MATRIX, ids=[f"{r.value}-{p.value}" for r, p in MATRIX])
async def test_require_dependency(probe_app, client, session, make_user, auth_headers, role, perm):
    user = await make_user(role)
    uid = user.id
    r = await client.get(f"/_probe/{perm.name}", headers=auth_headers(user))
    rows = await _audit(session, "authz.denied")
    if perm in EXPECTED[role]:
        assert r.status_code == 200
        assert rows == []
    else:
        assert r.status_code == 403
        assert len(rows) == 1
        row = rows[0]
        assert row.outcome == Outcome.denied and row.actor_user_id == uid
        assert row.metadata_ == {"permission": perm.value, "role": role.value}


async def test_role_read_from_db_not_token(probe_app, client, session, make_user, auth_headers):
    user = await make_user(Role.admin)
    headers = auth_headers(user)
    assert (await client.get("/_probe/audit_read", headers=headers)).status_code == 200
    user.role = Role.viewer
    await session.commit()
    assert (await client.get("/_probe/audit_read", headers=headers)).status_code == 403


# --- route-level enforcement ----------------------------------------------------------------

def _routes(target_id: int, grant_id: int):
    return [
        ("GET", "/admin/users", None, P.user_manage, 200),
        ("PATCH", f"/admin/users/{target_id}", {"role": "viewer"}, P.user_manage, 200),
        ("POST", f"/admin/users/{target_id}/grants", {"repo_full_name": "acme/new"}, P.grant_manage, 201),
        ("DELETE", f"/admin/users/{target_id}/grants/{grant_id}", None, P.grant_manage, 204),
        ("GET", "/audit", None, P.audit_read, 200),
    ]


@pytest.mark.parametrize("role", list(Role))
async def test_route_matrix(client, session, make_user, auth_headers, role):
    from app.models import RepoGrant

    actor = await make_user(role)
    if role != Role.admin:
        await make_user(Role.admin)  # so there is always an admin in the system
    target = await make_user(Role.reviewer)
    grant = RepoGrant(user_id=target.id, repo_full_name="acme/existing")
    session.add(grant)
    await session.commit()
    headers = auth_headers(actor)
    actor_id = actor.id

    for method, path, body, perm, ok_status in _routes(target.id, grant.id):
        before = len(await _audit(session, "authz.denied"))
        r = await client.request(method, path, json=body, headers=headers)
        after = await _audit(session, "authz.denied")
        if perm in EXPECTED[role]:
            assert r.status_code == ok_status, (method, path, r.text)
            assert len(after) == before
        else:
            assert r.status_code == 403, (method, path)
            assert len(after) == before + 1, (method, path)
            assert after[-1].actor_user_id == actor_id
            assert after[-1].metadata_["permission"] == perm.value


@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/users"), ("PATCH", "/admin/users/1"), ("POST", "/admin/users/1/grants"),
    ("DELETE", "/admin/users/1/grants/1"), ("GET", "/audit"), ("GET", "/auth/me"),
])
async def test_unauthenticated_routes_401(client, method, path):
    r = await client.request(method, path, json={"role": "viewer"} if method == "PATCH" else None)
    assert r.status_code == 401


async def test_forbidden_actor_cannot_learn_about_targets(client, make_user, auth_headers):
    """403 comes before any lookup: a reviewer gets the same answer for real and fake ids."""
    reviewer = await make_user(Role.reviewer)
    h = auth_headers(reviewer)
    assert (await client.patch("/admin/users/999999", json={"role": "admin"}, headers=h)).status_code == 403
    assert (await client.patch(f"/admin/users/{reviewer.id}", json={"role": "admin"}, headers=h)).status_code == 403


@pytest.mark.parametrize("role", list(Role))
async def test_me_lists_permissions(client, make_user, auth_headers, role):
    user = await make_user(role)
    r = await client.get("/auth/me", headers=auth_headers(user))
    assert r.status_code == 200
    assert r.json()["permissions"] == sorted(p.value for p in EXPECTED[role])
