"""User and grant administration. Every call is audited in the same commit as its effect,
with before/after state; refusals (409/404) are audited as denials."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.db import get_session
from app.deps import require
from app.models import Outcome, RepoGrant, Role, User
from app.rbac import Permission
from app.request_context import RequestContext, get_request_context
from app.user_schemas import GrantOut, UserOut
from app.validation import validate_repo_full_name

router = APIRouter(prefix="/admin", tags=["admin"])


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> "UserPatch":
        if self.role is None and self.is_active is None:
            raise ValueError("nothing to change")
        return self


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_full_name: str

    # Not `pattern=`: pydantic's Rust regex has no lookahead, and `.`/`..` must be rejected.
    @field_validator("repo_full_name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        return validate_repo_full_name(v, allow_wildcard=True)


def _user_state(u: User) -> dict[str, Any]:
    return {"role": u.role.value, "is_active": u.is_active, "has_github_token": u.github_token_enc is not None}


async def _get_user_or_404(
    session: AsyncSession, ctx: RequestContext, actor: User, user_id: int, action: str
) -> User:
    target = await session.get(User, user_id)
    if target is None:
        record_audit(
            session, ctx=ctx, actor=actor, action=action, outcome=Outcome.denied,
            resource_type="user", resource_id=user_id, metadata={"reason": "not_found"},
        )
        await session.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    return target


@router.get("/users", response_model=list[UserOut])
async def list_users(
    actor: User = Depends(require(Permission.user_manage)),
    session: AsyncSession = Depends(get_session),
    ctx: RequestContext = Depends(get_request_context),
) -> list[UserOut]:
    users = (await session.execute(select(User).order_by(User.id))).scalars().all()
    out = [UserOut.model_validate(u) for u in users]
    record_audit(
        session, ctx=ctx, actor=actor, action="admin.users.list", outcome=Outcome.success,
        resource_type="user", metadata={"count": len(out)},
    )
    await session.commit()
    return out


@router.patch("/users/{user_id}", response_model=UserOut)
async def patch_user(
    user_id: int,
    body: UserPatch,
    actor: User = Depends(require(Permission.user_manage)),
    session: AsyncSession = Depends(get_session),
    ctx: RequestContext = Depends(get_request_context),
) -> UserOut:
    action = "admin.user.update"
    target = await _get_user_or_404(session, ctx, actor, user_id, action)
    before = _user_state(target)
    requested = body.model_dump(exclude_none=True, mode="json")

    async def conflict(reason: str, detail: str) -> HTTPException:
        record_audit(
            session, ctx=ctx, actor=actor, action=action, outcome=Outcome.denied,
            resource_type="user", resource_id=target.id,
            metadata={"reason": reason, "before": before, "requested": requested},
        )
        await session.commit()
        return HTTPException(status.HTTP_409_CONFLICT, detail)

    new_role = body.role if body.role is not None else target.role
    new_active = body.is_active if body.is_active is not None else target.is_active

    loses_admin = target.role == Role.admin and target.is_active and (new_role != Role.admin or not new_active)

    # Last-admin guard first: with a single admin, a self-demotion is also the last-admin case.
    if loses_admin:
        # Lock the other active admin rows (no-op on SQLite) so two concurrent demotions
        # cannot both pass the check.
        others = (
            await session.execute(
                select(User.id)
                .where(User.role == Role.admin, User.is_active.is_(True), User.id != target.id)
                .with_for_update()
            )
        ).scalars().all()
        if not others:
            raise await conflict("last_admin", "cannot demote or deactivate the last active admin")

    if target.id == actor.id and loses_admin:
        raise await conflict("self_demote", "you cannot remove your own admin role or deactivate yourself")

    target.role = new_role
    target.is_active = new_active
    if not new_active:
        # A disabled account must not leave a working credential in a column.
        target.github_token_enc = None
    after = _user_state(target)

    record_audit(
        session, ctx=ctx, actor=actor, action=action, outcome=Outcome.success,
        resource_type="user", resource_id=target.id,
        metadata={"before": before, "after": after, "requested": requested},
    )
    await session.commit()
    return UserOut.model_validate(target)


@router.post("/users/{user_id}/grants", response_model=GrantOut, status_code=status.HTTP_201_CREATED)
async def create_grant(
    user_id: int,
    body: GrantIn,
    actor: User = Depends(require(Permission.grant_manage)),
    session: AsyncSession = Depends(get_session),
    ctx: RequestContext = Depends(get_request_context),
) -> GrantOut:
    action = "admin.grant.create"
    target = await _get_user_or_404(session, ctx, actor, user_id, action)
    name = body.repo_full_name
    before = sorted(g.repo_full_name for g in target.grants)

    async def duplicate() -> HTTPException:
        record_audit(
            session, ctx=ctx, actor=actor, action=action, outcome=Outcome.denied,
            resource_type="user", resource_id=user_id,
            metadata={"reason": "duplicate", "repo_full_name": name, "before": before},
        )
        await session.commit()
        return HTTPException(status.HTTP_409_CONFLICT, "grant already exists")

    # GitHub names are case-insensitive, so `acme/Repo` duplicates `acme/repo`.
    existing = (
        await session.execute(
            select(RepoGrant.id).where(
                RepoGrant.user_id == user_id, func.lower(RepoGrant.repo_full_name) == name.lower()
            )
        )
    ).first()
    if existing is not None:
        raise await duplicate()

    grant = RepoGrant(user_id=user_id, repo_full_name=name, created_by_user_id=actor.id)
    session.add(grant)
    try:
        await session.flush()
    except IntegrityError:
        # Lost a race with a concurrent identical grant. Rollback expires loaded objects;
        # reload the actor (async-safe) before auditing the denial.
        await session.rollback()
        await session.refresh(actor)
        raise await duplicate()

    record_audit(
        session, ctx=ctx, actor=actor, action=action, outcome=Outcome.success,
        resource_type="repo_grant", resource_id=grant.id,
        metadata={
            "user_id": user_id, "repo_full_name": name,
            "before": before, "after": sorted([*before, name]),
        },
    )
    await session.commit()
    return GrantOut.model_validate(grant)


@router.delete(
    "/users/{user_id}/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
async def delete_grant(
    user_id: int,
    grant_id: int,
    actor: User = Depends(require(Permission.grant_manage)),
    session: AsyncSession = Depends(get_session),
    ctx: RequestContext = Depends(get_request_context),
) -> Response:
    action = "admin.grant.delete"
    target = await _get_user_or_404(session, ctx, actor, user_id, action)
    grant = await session.get(RepoGrant, grant_id)
    if grant is None or grant.user_id != user_id:
        record_audit(
            session, ctx=ctx, actor=actor, action=action, outcome=Outcome.denied,
            resource_type="repo_grant", resource_id=grant_id,
            metadata={"reason": "not_found", "user_id": user_id},
        )
        await session.commit()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "grant not found")

    before = sorted(g.repo_full_name for g in target.grants)
    removed = grant.repo_full_name
    await session.delete(grant)
    after = [n for n in before if n != removed]  # names are unique per user
    record_audit(
        session, ctx=ctx, actor=actor, action=action, outcome=Outcome.success,
        resource_type="repo_grant", resource_id=grant_id,
        metadata={"user_id": user_id, "repo_full_name": removed, "before": before, "after": after},
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
