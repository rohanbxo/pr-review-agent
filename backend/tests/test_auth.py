"""Token exchange, sign-in policy, admin guards and grant-name validation."""

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select

import tests.helpers.shims  # noqa: F401  (must precede app imports)
from app import github_app
from app.config import get_settings
from app.github_identity import can_read_repo, fetch_identity, get_github_transport, GitHubIdentityError
from app.models import AuditLog, Outcome, RepoGrant, Role, User
from app.security import decrypt_token
from tests.helpers.github_identity import FakeGitHub

SECRET = "test-bridge-secret"
EXCHANGE = "/auth/github/exchange"


@pytest.fixture
def github(app):
    fake = FakeGitHub()
    app.dependency_overrides[get_github_transport] = lambda: fake.transport
    return fake


async def exchange(client, token, *, secret=SECRET, body=None):
    headers = {} if secret is None else {"X-Auth-Bridge-Secret": secret}
    return await client.post(EXCHANGE, json=body if body is not None else {"access_token": token}, headers=headers)


async def audit_rows(session, action=None):
    stmt = select(AuditLog).order_by(AuditLog.id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    session.expire_all()
    return (await session.execute(stmt)).scalars().all()


async def user_count(session):
    return (await session.execute(select(func.count()).select_from(User))).scalar_one()


async def reload(session, user_id) -> User:
    session.expire_all()
    return await session.get(User, user_id)


# --- bridge secret -------------------------------------------------------------------------

@pytest.mark.parametrize("secret", [None, "", "wrong", SECRET + "x", SECRET.upper()])
async def test_bad_bridge_secret_rejected_and_audited(client, session, github, secret):
    github.add("tok", id=1, login="boss")
    r = await exchange(client, "tok", secret=secret)
    assert r.status_code == 401
    assert await user_count(session) == 0
    assert github.requests == []  # GitHub never consulted without the secret
    rows = await audit_rows(session, "auth.exchange")
    assert len(rows) == 1 and rows[0].outcome == Outcome.denied
    assert rows[0].metadata_["reason"] == "bad_bridge_secret"


async def test_bridge_secret_checked_before_body(client, github):
    r = await exchange(client, None, secret="wrong", body={"access_token": "t", "role": "admin"})
    assert r.status_code == 401


# --- the backend never trusts a profile -----------------------------------------------------

@pytest.mark.parametrize("extra", [
    {"login": "boss"}, {"role": "admin"}, {"github_id": 1}, {"email": "boss@example.com"},
    {"orgs": ["acme"]},
])
async def test_extra_profile_fields_rejected(client, session, github, extra):
    github.add("tok", id=1, login="nobody")
    r = await exchange(client, None, body={"access_token": "tok", **extra})
    assert r.status_code == 422
    assert await user_count(session) == 0
    assert github.requests == []


@pytest.mark.parametrize("body", [{}, {"access_token": ""}, {"access_token": 123}])
async def test_malformed_body_rejected(client, github, body):
    r = await exchange(client, None, body=body)
    assert r.status_code == 422


async def test_identity_is_reread_from_github(client, session, github):
    github.add("tok", id=4242, login="boss", email="boss@corp.example", orgs=["acme"])
    r = await exchange(client, "tok")
    assert r.status_code == 200
    u = r.json()["user"]
    assert u["github_id"] == 4242 and u["github_login"] == "boss"
    # Verified primary address from /user/emails, never the unverified profile email.
    assert u["email"] == "boss@corp.example"
    paths = [req.url.path for req in github.requests]
    assert {"/user", "/user/emails", "/user/orgs"} <= set(paths)
    assert all(req.headers["authorization"] == "Bearer tok" for req in github.requests)


async def test_invalid_github_token_rejected_and_audited(client, session, github):
    r = await exchange(client, "not-a-real-token")
    assert r.status_code == 401
    assert await user_count(session) == 0
    rows = await audit_rows(session, "auth.exchange")
    assert rows[-1].outcome == Outcome.denied
    assert rows[-1].metadata_["reason"] == "github_verification_failed"
    assert "not-a-real-token" not in json.dumps(rows[-1].metadata_)


# --- fail closed ----------------------------------------------------------------------------

async def test_unknown_account_rejected_and_audited(client, session, github):
    github.add("tok", id=77, login="stranger", orgs=["some-other-org"])
    r = await exchange(client, "tok")
    assert r.status_code == 403
    assert await user_count(session) == 0  # not provisioned as a viewer
    rows = await audit_rows(session, "auth.exchange")
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome == Outcome.denied and row.actor_user_id is None
    assert row.metadata_ == {"reason": "unknown_account", "github_id": 77, "github_login": "stranger"}
    assert row.actor_ip == "203.0.113.7"


# --- role derivation at first sign-in -------------------------------------------------------

@pytest.mark.parametrize("gid,login,orgs,role", [
    (500, "boss", [], "admin"),                   # GITHUB_ADMIN_IDS=500 in conftest
    (500, "renamed-boss", ["acme-readers"], "admin"),  # admin id wins; the login is irrelevant
    (501, "alice", ["acme"], "reviewer"),
    (501, "alice", ["ACME", "acme-readers"], "reviewer"),
    (502, "bob", ["acme-readers"], "viewer"),
])
async def test_first_sign_in_role_derivation(client, session, github, gid, login, orgs, role):
    github.add("tok", id=gid, login=login, orgs=orgs)
    r = await exchange(client, "tok")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["role"] == role
    u = await reload(session, body["user"]["id"])
    assert u.role.value == role and u.last_login_at is not None
    uid = u.id
    rows = await audit_rows(session, "auth.exchange")
    assert rows[-1].outcome == Outcome.success
    assert rows[-1].metadata_["first_sign_in"] is True and rows[-1].actor_user_id == uid


async def test_admin_is_bound_to_github_id_not_login(client, session, github):
    """Someone who registers a former admin's login (after a rename) must not inherit admin."""
    github.add("tok", id=9999, login="boss", orgs=[])
    r = await exchange(client, "tok")
    assert r.status_code == 403
    assert await user_count(session) == 0


async def test_role_not_rederived_on_later_login(client, session, github, make_user, auth_headers):
    admin = await make_user(Role.admin, login="root")
    github.add("tok", id=600, login="alice", orgs=["acme"])
    r = await exchange(client, "tok")
    uid = r.json()["user"]["id"]
    assert r.json()["user"]["role"] == "reviewer"

    r = await client.patch(f"/admin/users/{uid}", json={"role": "viewer"}, headers=auth_headers(admin))
    assert r.status_code == 200

    r = await exchange(client, "tok")
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "viewer"
    assert (await reload(session, uid)).role == Role.viewer
    rows = await audit_rows(session, "auth.exchange")
    assert rows[-1].metadata_["first_sign_in"] is False


async def test_manual_promotion_survives_login(client, session, github, make_user):
    viewer = await make_user(Role.admin, login="alice", github_id=601)  # promoted manually earlier
    github.add("tok", id=601, login="alice", orgs=["acme-readers"])
    r = await exchange(client, "tok")
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "admin"
    assert (await reload(session, viewer.id)).role == Role.admin


async def test_profile_fields_refreshed_on_login(client, session, github, make_user):
    u = await make_user(Role.reviewer, login="oldname", github_id=602, email="old@example.com")
    github.add("tok", id=602, login="newname", orgs=["acme"], email="new@example.com")
    r = await exchange(client, "tok")
    assert r.status_code == 200
    u = await reload(session, u.id)
    assert (u.github_login, u.email) == ("newname", "new@example.com")
    assert u.avatar_url == "https://avatars.example/602"
    assert u.last_login_at is not None
    assert await user_count(session) == 1  # keyed on github_id, not login


async def test_org_removal_blocks_login(client, session, github):
    acct = github.add("tok", id=700, login="carol", orgs=["acme"])
    r = await exchange(client, "tok")
    uid = r.json()["user"]["id"]
    assert r.status_code == 200

    acct.orgs = []  # removed from the org on GitHub
    r = await exchange(client, "tok")
    assert r.status_code == 403
    u = await reload(session, uid)
    assert u.role == Role.reviewer          # role untouched
    assert u.github_token_enc is None       # credential dropped
    rows = await audit_rows(session, "auth.exchange")
    assert rows[-1].outcome == Outcome.denied
    assert rows[-1].metadata_["reason"] == "no_longer_authorised"
    assert rows[-1].actor_user_id == uid


async def test_inactive_user_blocked(client, session, github, make_user):
    u = await make_user(Role.reviewer, login="dave", github_id=800, is_active=False)
    github.add("tok", id=800, login="dave", orgs=["acme"])
    r = await exchange(client, "tok")
    assert r.status_code == 403
    uid = u.id
    rows = await audit_rows(session, "auth.exchange")
    assert rows[-1].outcome == Outcome.denied and rows[-1].metadata_["reason"] == "inactive"
    assert (await reload(session, uid)).github_token_enc is None


async def test_token_stored_encrypted(client, session, github):
    github.add("gho_supersecret", id=900, login="erin", orgs=["acme"])
    r = await exchange(client, "gho_supersecret")
    u = await reload(session, r.json()["user"]["id"])
    assert u.github_token_enc and "gho_supersecret" not in u.github_token_enc
    assert decrypt_token(u.github_token_enc) == "gho_supersecret"
    assert "gho_supersecret" not in r.text
    for row in await audit_rows(session):
        assert "gho_supersecret" not in json.dumps(row.metadata_)


async def test_jwt_works_on_me(client, github):
    github.add("tok", id=1000, login="frank", orgs=["acme"])
    r = await exchange(client, "tok")
    body = r.json()
    assert set(body) == {"token", "expires_at", "user"}
    claims = jwt.decode(body["token"], options={"verify_signature": False})
    assert set(claims) == {"sub", "iat", "exp", "iss"}  # no role in the token
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    m = me.json()
    assert m["github_login"] == "frank" and m["role"] == "reviewer"
    assert m["permissions"] == ["review:create", "review:read"]
    assert "github_token_enc" not in m


async def test_me_rejects_bad_tokens(client, make_user, auth_headers):
    assert (await client.get("/auth/me")).status_code == 401
    assert (await client.get("/auth/me", headers={"Authorization": "Bearer garbage"})).status_code == 401
    forged = jwt.encode({"sub": "1", "iat": int(time.time()), "exp": int(time.time()) + 60, "iss": "pr-review-agent"},
                        "wrong-secret-wrong-secret-wrong-secret", algorithm="HS256")
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})).status_code == 401
    inactive = await make_user(Role.admin, is_active=False)
    assert (await client.get("/auth/me", headers=auth_headers(inactive))).status_code == 401


# --- admin guards ---------------------------------------------------------------------------

async def test_last_admin_lockout(client, session, make_user, auth_headers):
    admin = await make_user(Role.admin)
    for body in ({"role": "reviewer"}, {"role": "viewer"}, {"is_active": False}):
        r = await client.patch(f"/admin/users/{admin.id}", json=body, headers=auth_headers(admin))
        assert r.status_code == 409, body
    assert (await reload(session, admin.id)).role == Role.admin
    rows = await audit_rows(session, "admin.user.update")
    assert len(rows) == 3
    assert all(r.outcome == Outcome.denied and r.metadata_["reason"] == "last_admin" for r in rows)


async def test_self_demote_forbidden_even_with_other_admins(client, session, make_user, auth_headers):
    a1 = await make_user(Role.admin)
    a2 = await make_user(Role.admin)
    a1_id, a2_id, h1 = a1.id, a2.id, auth_headers(a1)
    r = await client.patch(f"/admin/users/{a1_id}", json={"role": "viewer"}, headers=h1)
    assert r.status_code == 409
    rows = await audit_rows(session, "admin.user.update")
    assert rows[-1].metadata_["reason"] == "self_demote"
    # Demoting the OTHER admin is fine while one remains...
    r = await client.patch(f"/admin/users/{a2_id}", json={"role": "reviewer"}, headers=h1)
    assert r.status_code == 200 and r.json()["role"] == "reviewer"
    # ...and a no-op admin->admin patch on yourself is allowed.
    r = await client.patch(f"/admin/users/{a1_id}", json={"role": "admin"}, headers=h1)
    assert r.status_code == 200


async def test_inactive_admins_do_not_count_toward_last_admin(client, make_user, auth_headers):
    a1 = await make_user(Role.admin)
    await make_user(Role.admin, is_active=False)
    r = await client.patch(f"/admin/users/{a1.id}", json={"role": "viewer"}, headers=auth_headers(a1))
    assert r.status_code == 409


async def test_deactivation_clears_token(client, session, make_user, auth_headers):
    from app.security import encrypt_token

    admin = await make_user(Role.admin)
    target = await make_user(Role.reviewer, github_token_enc=encrypt_token("gho_x"))
    r = await client.patch(f"/admin/users/{target.id}", json={"is_active": False}, headers=auth_headers(admin))
    assert r.status_code == 200 and r.json()["is_active"] is False
    u = await reload(session, target.id)
    assert u.github_token_enc is None
    # And the deactivated user's existing JWT stops working immediately.
    assert (await client.get("/auth/me", headers=auth_headers(u))).status_code == 401


async def test_patch_validation(client, make_user, auth_headers):
    admin = await make_user(Role.admin)
    target = await make_user(Role.viewer)
    h = auth_headers(admin)
    assert (await client.patch(f"/admin/users/{target.id}", json={}, headers=h)).status_code == 422
    assert (await client.patch(f"/admin/users/{target.id}", json={"role": "superuser"}, headers=h)).status_code == 422
    assert (await client.patch(f"/admin/users/{target.id}",
                               json={"role": "viewer", "github_token_enc": None}, headers=h)).status_code == 422
    assert (await client.patch("/admin/users/999999", json={"role": "viewer"}, headers=h)).status_code == 404


# --- grant names ----------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "../etc", ".", "..", "-bad/x", "acme/.", "acme/..", "acme", "acme/x/y", "acme/", "/x",
    "ac me/x", "acme/re po", "*/*", "acme/../etc", "a" * 40 + "/x",
])
async def test_bad_grant_names_rejected(client, session, make_user, auth_headers, name):
    admin = await make_user(Role.admin)
    target = await make_user(Role.reviewer)
    r = await client.post(f"/admin/users/{target.id}/grants", json={"repo_full_name": name},
                          headers=auth_headers(admin))
    assert r.status_code == 422, name
    assert (await session.execute(select(func.count()).select_from(RepoGrant))).scalar_one() == 0


@pytest.mark.parametrize("name", ["acme/.github", "acme/*", "acme/repo", "a-b/c_d.e-f", "A1/..x"])
async def test_good_grant_names_accepted(client, make_user, auth_headers, name):
    admin = await make_user(Role.admin)
    target = await make_user(Role.reviewer)
    r = await client.post(f"/admin/users/{target.id}/grants", json={"repo_full_name": name},
                          headers=auth_headers(admin))
    assert r.status_code == 201, r.text
    assert r.json()["repo_full_name"] == name


async def test_duplicate_grant_conflicts(client, session, make_user, auth_headers):
    admin = await make_user(Role.admin)
    target = await make_user(Role.reviewer)
    h = auth_headers(admin)
    url = f"/admin/users/{target.id}/grants"
    assert (await client.post(url, json={"repo_full_name": "acme/repo"}, headers=h)).status_code == 201
    assert (await client.post(url, json={"repo_full_name": "acme/repo"}, headers=h)).status_code == 409
    assert (await client.post(url, json={"repo_full_name": "ACME/Repo"}, headers=h)).status_code == 409
    rows = await audit_rows(session, "admin.grant.create")
    assert [r.outcome for r in rows] == [Outcome.success, Outcome.denied, Outcome.denied]


async def test_delete_grant(client, session, make_user, auth_headers):
    admin = await make_user(Role.admin)
    target = await make_user(Role.reviewer)
    other = await make_user(Role.reviewer)
    h = auth_headers(admin)
    gid = (await client.post(f"/admin/users/{target.id}/grants", json={"repo_full_name": "acme/*"},
                             headers=h)).json()["id"]
    # A grant can only be deleted through its owner.
    assert (await client.delete(f"/admin/users/{other.id}/grants/{gid}", headers=h)).status_code == 404
    assert (await client.delete(f"/admin/users/{target.id}/grants/{gid}", headers=h)).status_code == 204
    assert (await client.delete(f"/admin/users/{target.id}/grants/{gid}", headers=h)).status_code == 404
    users = (await client.get("/admin/users", headers=h)).json()
    assert next(u for u in users if u["id"] == target.id)["grants"] == []


# --- github_identity / github_app units -----------------------------------------------------

async def test_fetch_identity_rejects_malformed_user():
    t = httpx.MockTransport(lambda req: httpx.Response(200, json={"login": "x"}))
    with pytest.raises(GitHubIdentityError):
        await fetch_identity("tok", transport=t)
    t = httpx.MockTransport(lambda req: httpx.Response(500))
    with pytest.raises(GitHubIdentityError):
        await fetch_identity("tok", transport=t)


async def test_fetch_identity_follows_org_pagination():
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/user":
            return httpx.Response(200, json={"id": 1, "login": "pat"})
        if p == "/user/emails":
            return httpx.Response(200, json=[])
        if p == "/user/orgs" and req.url.params.get("page") != "2":
            return httpx.Response(200, json=[{"login": "one"}],
                                  headers={"Link": '<https://api.github.com/user/orgs?page=2>; rel="next"'})
        if p == "/user/orgs":
            return httpx.Response(200, json=[{"login": "two"}])
        return httpx.Response(404)

    ident = await fetch_identity("tok", transport=httpx.MockTransport(handler))
    assert ident.orgs == ["one", "two"] and ident.email is None


def _repo_transport(*, private: bool, permission: str | None = "read", perm_status=200, seen=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        if req.url.path == "/repos/acme/app":
            return httpx.Response(200, json={"private": private, "visibility": "private" if private else "public"})
        if req.url.path == "/repos/acme/app/collaborators/alice/permission":
            return httpx.Response(perm_status, json={"permission": permission})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def readonly_token(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "github_app_id", "")
    monkeypatch.setattr(s, "github_readonly_token", "ro-token")
    return s


async def test_can_read_repo(readonly_token):
    kw = dict(user_login="alice", repo_full_name="acme/app")
    assert await can_read_repo(**kw, transport=_repo_transport(private=False)) is True
    assert await can_read_repo(**kw, transport=_repo_transport(private=True, permission="read")) is True
    assert await can_read_repo(**kw, transport=_repo_transport(private=True, permission="admin")) is True
    assert await can_read_repo(**kw, transport=_repo_transport(private=True, permission="none")) is False
    assert await can_read_repo(**kw, transport=_repo_transport(private=True, perm_status=404)) is False
    assert await can_read_repo(**kw, transport=httpx.MockTransport(lambda r: httpx.Response(500))) is False

    def boom(req):
        raise httpx.ConnectError("down")

    assert await can_read_repo(**kw, transport=httpx.MockTransport(boom)) is False
    # Invalid input never reaches GitHub.
    seen: list = []
    t = _repo_transport(private=False, seen=seen)
    assert await can_read_repo(user_login="alice", repo_full_name="../etc", transport=t) is False
    assert await can_read_repo(user_login="al/ice", repo_full_name="acme/app", transport=t) is False
    assert seen == []


async def test_can_read_repo_private_without_token_fails_closed(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "github_app_id", "")
    monkeypatch.setattr(s, "github_readonly_token", "")
    assert await can_read_repo(user_login="alice", repo_full_name="acme/app",
                               transport=_repo_transport(private=True)) is False


async def test_installation_token_minted_and_cached(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    s = get_settings()
    monkeypatch.setattr(s, "github_app_id", "12345")
    monkeypatch.setattr(s, "github_app_private_key", pem.replace("\n", "\\n"))  # env-style escaped
    monkeypatch.setattr(s, "github_app_installation_id", "678")
    github_app.reset_cache()
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        assert req.method == "POST" and req.url.path == "/app/installations/678/access_tokens"
        claims = jwt.decode(req.headers["authorization"].removeprefix("Bearer "), key.public_key(),
                            algorithms=["RS256"])
        assert claims["iss"] == "12345" and claims["exp"] - claims["iat"] <= 600
        assert json.loads(req.content) == {"permissions": github_app.READ_ONLY_PERMISSIONS}
        return httpx.Response(201, json={"token": "ghs_inst", "expires_at": "2999-01-01T00:00:00Z"})

    t = httpx.MockTransport(handler)
    try:
        assert await github_app.get_installation_token(transport=t) == "ghs_inst"
        assert await github_app.get_installation_token(transport=t) == "ghs_inst"
        assert len(calls) == 1
    finally:
        github_app.reset_cache()


async def test_installation_token_falls_back_to_dev_token(readonly_token):
    assert await github_app.get_installation_token() == "ro-token"
