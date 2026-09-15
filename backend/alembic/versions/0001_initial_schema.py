"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-15

Mirrors app/models.py exactly (verified with `alembic check` against Postgres 16).
The downgrade drops the enum types explicitly: op.drop_table does not.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# create_type=False: types are created/dropped explicitly below so up/down/up is clean.
user_role = postgresql.ENUM("admin", "reviewer", "viewer", name="user_role", create_type=False)
run_status = postgresql.ENUM("queued", "running", "succeeded", "failed", name="run_status", create_type=False)
step_kind = postgresql.ENUM("node", "github_calls", "error", name="step_kind", create_type=False)
audit_outcome = postgresql.ENUM("success", "denied", "error", name="audit_outcome", create_type=False)
_ENUMS = (user_role, run_status, step_kind, audit_outcome)

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    bind = op.get_bind()
    for e in _ENUMS:
        e.create(bind, checkfirst=False)

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("role", user_role, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("github_token_enc", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("github_id"),
    )
    op.create_index("ix_users_github_login", "users", ["github_login"], unique=False)

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_email", sa.String(length=320), nullable=True),
        sa.Column("actor_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=50), nullable=True),
        sa.Column("resource_id", sa.String(length=200), nullable=True),
        sa.Column("outcome", audit_outcome, nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("metadata", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"], unique=False)
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"], unique=False)
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"], unique=False)
    op.create_index("ix_audit_logs_metadata_gin", "audit_logs", ["metadata"], unique=False, postgresql_using="gin")
    op.create_index("ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id"], unique=False)

    op.create_table(
        "repo_grants",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("repo_full_name", sa.String(length=140), nullable=False),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "repo_full_name", name="uq_repo_grants_user_repo"),
    )
    op.create_index("ix_repo_grants_user_id", "repo_grants", ["user_id"], unique=False)

    op.create_table(
        "review_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("repo_full_name", sa.String(length=140), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("status", run_status, nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("langfuse_trace_id", sa.String(length=64), nullable=True),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("usage", JSONB, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_runs_created_at", "review_runs", ["created_at"], unique=False)
    op.create_index("ix_review_runs_repo_pr", "review_runs", ["repo_full_name", "pr_number"], unique=False)
    op.create_index("ix_review_runs_result_gin", "review_runs", ["result"], unique=False, postgresql_using="gin")
    op.create_index("ix_review_runs_status", "review_runs", ["status"], unique=False)
    op.create_index("ix_review_runs_user_id", "review_runs", ["user_id"], unique=False)

    op.create_table(
        "agent_steps",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", step_kind, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("input", JSONB, nullable=True),
        sa.Column("output", JSONB, nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["review_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "seq", name="uq_agent_steps_run_seq"),
    )
    op.create_index("ix_agent_steps_run_id", "agent_steps", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_agent_steps_run_id", table_name="agent_steps")
    op.drop_table("agent_steps")
    op.drop_index("ix_review_runs_user_id", table_name="review_runs")
    op.drop_index("ix_review_runs_status", table_name="review_runs")
    op.drop_index("ix_review_runs_result_gin", table_name="review_runs", postgresql_using="gin")
    op.drop_index("ix_review_runs_repo_pr", table_name="review_runs")
    op.drop_index("ix_review_runs_created_at", table_name="review_runs")
    op.drop_table("review_runs")
    op.drop_index("ix_repo_grants_user_id", table_name="repo_grants")
    op.drop_table("repo_grants")
    op.drop_index("ix_audit_logs_resource", table_name="audit_logs")
    op.drop_index("ix_audit_logs_metadata_gin", table_name="audit_logs", postgresql_using="gin")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_user_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_users_github_login", table_name="users")
    op.drop_table("users")

    bind = op.get_bind()
    for e in reversed(_ENUMS):
        e.drop(bind, checkfirst=False)
