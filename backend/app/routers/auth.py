"""Token exchange: Auth.js (server side) swaps a GitHub OAuth access token for a backend JWT.

- The caller must present the shared bridge secret (constant-time compare).
- The body carries ONLY the access token. Any profile the caller might send is rejected; the
  identity is re-read from GitHub.
- Sign-in fails closed: accounts matching no admin login / allowed org are rejected.
- Role is derived at FIRST sign-in only. Later sign-ins re-check that the account is still
  allowed (org membership changes) but never touch the role.
"""

import hmac
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.config import Settings, get_settings
from app.db import get_session
from app.deps import get_current_user
from app.github_identity import GitHubIdentity, GitHubIdentityError, fetch_identity, get_github_transport
from app.models import Outcome, Role, User
from app.rbac import permissions_for
from app.request_context import RequestContext, get_request_context
from app.security import encrypt_token, mint_jwt
from app.user_schemas import MeOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])

ACTION = "auth.exchange"


class ExchangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str = Field(min_length=1, max_length=4096)


class ExchangeOut(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut


def derive_role(identity: GitHubIdentity, settings: Settings) -> Role | None:
    """Role implied by the sign-in policy, or None if the account is not allowed at all.
    GitHub logins and org names are case-insensitive."""
    orgs = {o.lower() for o in identity.orgs}
    # Admin bootstrap matches the immutable numeric id, not the login (renames get squatted).
    if str(identity.id) in {a.strip() for a in settings.github_admin_ids}:
        return Role.admin
    if orgs & {o.lower() for o in settings.github_reviewer_orgs}:
        return Role.reviewer
    if orgs & {o.lower() for o in settings.github_viewer_orgs}:
        return Role.viewer
    return None


async def verify_bridge_secret(
    x_auth_bridge_secret: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    ctx: RequestContext = Depends(get_request_context),
) -> None:
    """Runs before the body is validated, so a caller without the secret learns nothing."""
    expected = get_settings().auth_bridge_secret
    given = x_auth_bridge_secret or ""
    # Always run the comparison (no early return on a missing header); an unset secret fails closed.
    ok = hmac.compare_digest(given.encode(), expected.encode()) and bool(expected)
    if not ok:
        record_audit(
            session, ctx=ctx, actor=None, action=ACTION, outcome=Outcome.denied,
            metadata={"reason": "bad_bridge_secret", "header_present": x_auth_bridge_secret is not None},
        )
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bridge secret")


async def _deny(
    session: AsyncSession, ctx: RequestContext, *, actor: User | None, reason: str,
    code: int, detail: str, extra: dict | None = None,
) -> HTTPException:
    record_audit(
        session, ctx=ctx, actor=actor, action=ACTION, outcome=Outcome.denied,
        resource_type="user", resource_id=actor.id if actor else None,
        metadata={"reason": reason, **(extra or {})},
    )
    await session.commit()
    return HTTPException(code, detail)


@router.post("/github/exchange", response_model=ExchangeOut, dependencies=[Depends(verify_bridge_secret)])
async def github_exchange(
    body: ExchangeIn,
    session: AsyncSession = Depends(get_session),
    ctx: RequestContext = Depends(get_request_context),
    transport: httpx.AsyncBaseTransport | None = Depends(get_github_transport),
) -> ExchangeOut:
    settings = get_settings()
    try:
        identity = await fetch_identity(body.access_token, transport=transport)
    except GitHubIdentityError as e:
        raise await _deny(
            session, ctx, actor=None, reason="github_verification_failed",
            code=status.HTTP_401_UNAUTHORIZED, detail="GitHub did not verify the access token",
            extra={"error": str(e)[:200]},
        )

    who = {"github_id": identity.id, "github_login": identity.login}
    derived = derive_role(identity, settings)
    user = (
        await session.execute(select(User).where(User.github_id == identity.id))
    ).scalar_one_or_none()
    first_sign_in = user is None

    if user is None:
        if derived is None:
            raise await _deny(
                session, ctx, actor=None, reason="unknown_account",
                code=status.HTTP_403_FORBIDDEN, detail="account is not authorised", extra=who,
            )
        user = User(github_id=identity.id, github_login=identity.login, role=derived, is_active=True)
        session.add(user)
    else:
        if not user.is_active:
            raise await _deny(
                session, ctx, actor=user, reason="inactive",
                code=status.HTTP_403_FORBIDDEN, detail="account is deactivated", extra=who,
            )
        if derived is None:
            # No longer in any allowed org / admin list. The role is kept (it is never
            # re-derived) but sign-in is refused and the stored credential is dropped.
            user.github_token_enc = None
            raise await _deny(
                session, ctx, actor=user, reason="no_longer_authorised",
                code=status.HTTP_403_FORBIDDEN, detail="account is not authorised", extra=who,
            )

    user.github_login = identity.login
    user.email = identity.email
    user.avatar_url = identity.avatar_url
    user.last_login_at = datetime.now(timezone.utc)
    user.github_token_enc = encrypt_token(body.access_token)
    await session.flush()

    record_audit(
        session, ctx=ctx, actor=user, action=ACTION, outcome=Outcome.success,
        resource_type="user", resource_id=user.id,
        metadata={**who, "first_sign_in": first_sign_in, "role": user.role.value},
    )
    await session.commit()
    await session.refresh(user, attribute_names=["grants"])

    token, exp = mint_jwt(user.id)
    return ExchangeOut(token=token, expires_at=exp, user=UserOut.model_validate(user))


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(get_current_user)) -> MeOut:
    return MeOut(**UserOut.model_validate(user).model_dump(), permissions=permissions_for(user.role))
