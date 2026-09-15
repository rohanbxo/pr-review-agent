"""Response/request models for identity, admin and audit routes (workstream A)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import AuditLog, Outcome, Role, User


class GrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repo_full_name: str
    created_at: datetime


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    github_id: int
    github_login: str
    email: str | None
    avatar_url: str | None
    role: Role
    is_active: bool
    last_login_at: datetime | None
    grants: list[GrantOut] = Field(default_factory=list)


class MeOut(UserOut):
    permissions: list[str]


class AuditOut(BaseModel):
    id: int
    created_at: datetime
    actor_user_id: int | None
    actor_email: str | None
    actor_ip: str | None
    user_agent: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    outcome: Outcome
    request_id: str | None
    metadata: dict[str, Any] | None

    @classmethod
    def from_row(cls, row: AuditLog) -> "AuditOut":
        return cls(
            id=row.id, created_at=row.created_at, actor_user_id=row.actor_user_id,
            actor_email=row.actor_email, actor_ip=row.actor_ip, user_agent=row.user_agent,
            action=row.action, resource_type=row.resource_type, resource_id=row.resource_id,
            outcome=row.outcome, request_id=row.request_id, metadata=row.metadata_,
        )


def user_out(user: User) -> UserOut:
    return UserOut.model_validate(user)
