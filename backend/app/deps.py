from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.db import get_session
from app.models import Outcome, User
from app.ratelimit import enforce_general_limit
from app.rbac import Permission, has_permission
from app.redis_client import get_redis
from app.request_context import RequestContext, get_request_context
from app.security import TokenError, decode_jwt

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> User:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED, "not authenticated", headers={"WWW-Authenticate": "Bearer"}
    )
    if creds is None or creds.scheme.lower() != "bearer":
        raise unauthorized
    try:
        user_id = decode_jwt(creds.credentials)
    except TokenError:
        raise unauthorized
    user = await session.get(User, user_id)
    # Role and is_active come from the DB on every request, never from the token.
    if user is None or not user.is_active:
        raise unauthorized
    await enforce_general_limit(user.id, redis)
    return user


def require(permission: Permission):
    """Route dependency: `user: User = Depends(require(Permission.review_create))`.
    Denials are audited and committed (there is no action to share a transaction with)."""

    async def _dep(
        user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
        ctx: RequestContext = Depends(get_request_context),
    ) -> User:
        if not has_permission(user.role, permission):
            record_audit(
                session, ctx=ctx, actor=user, action="authz.denied", outcome=Outcome.denied,
                metadata={"permission": permission.value, "role": user.role.value},
            )
            await session.commit()
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        return user

    return _dep
