"""The one permission matrix. Routes ask for a Permission, never a role name."""

import enum

from app.models import Role


class Permission(str, enum.Enum):
    review_create = "review:create"
    review_read = "review:read"
    review_read_all = "review:read_all"
    audit_read = "audit:read"
    user_manage = "user:manage"
    grant_manage = "grant:manage"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.admin: frozenset(Permission),
    Role.reviewer: frozenset({Permission.review_create, Permission.review_read}),
    Role.viewer: frozenset({Permission.review_read}),
}


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def permissions_for(role: Role) -> list[str]:
    return sorted(p.value for p in ROLE_PERMISSIONS.get(role, frozenset()))
