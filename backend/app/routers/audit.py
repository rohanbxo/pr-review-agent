"""Audit log reader. Keyset pagination on id (newest first): pass the last id you received as
`before_id` to get the next page."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import require
from app.models import AuditLog, User
from app.rbac import Permission
from app.user_schemas import AuditOut

router = APIRouter(tags=["audit"])


@router.get("/audit", response_model=list[AuditOut])
async def list_audit(
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, ge=1),
    action: str | None = Query(None, min_length=1, max_length=100),
    _: User = Depends(require(Permission.audit_read)),
    session: AsyncSession = Depends(get_session),
) -> list[AuditOut]:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if before_id is not None:
        stmt = stmt.where(AuditLog.id < before_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    rows = (await session.execute(stmt)).scalars().all()
    return [AuditOut.from_row(r) for r in rows]
