"""Audit helper. It NEVER commits: callers commit, so the audit row lands in the same
transaction as the action it describes. Denials are audited too."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, Outcome, User
from app.request_context import RequestContext


def record_audit(
    session: AsyncSession,
    *,
    ctx: RequestContext | None,
    actor: User | None,
    action: str,
    outcome: Outcome,
    resource_type: str | None = None,
    resource_id: str | int | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    row = AuditLog(
        actor_user_id=actor.id if actor else None,
        actor_email=(actor.email or actor.github_login) if actor else None,
        actor_ip=ctx.ip if ctx else None,
        user_agent=ctx.user_agent if ctx else None,
        request_id=ctx.request_id if ctx else None,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        outcome=outcome,
        metadata_=metadata,
    )
    session.add(row)
    return row
