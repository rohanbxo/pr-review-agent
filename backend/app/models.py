import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, JSONType

# SQLite only autoincrements INTEGER PRIMARY KEY.
PK = BigInteger().with_variant(Integer(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    admin = "admin"
    reviewer = "reviewer"
    viewer = "viewer"


class RunStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class StepKind(str, enum.Enum):
    node = "node"              # one row per LangGraph node update
    github_calls = "github_calls"  # the GitHub call log, persisted whatever the outcome
    error = "error"


class Outcome(str, enum.Enum):
    success = "success"
    denied = "denied"
    error = "error"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    return Enum(e, name=name, values_callable=lambda x: [m.value for m in x])


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    # The stable key: logins get renamed.
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    github_login: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(320))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    role: Mapped[Role] = mapped_column(_enum(Role, "user_role"), nullable=False, default=Role.viewer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    github_token_enc: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    grants: Mapped[list["RepoGrant"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin",
        foreign_keys="RepoGrant.user_id",
    )


class RepoGrant(Base):
    __tablename__ = "repo_grants"
    __table_args__ = (UniqueConstraint("user_id", "repo_full_name", name="uq_repo_grants_user_repo"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # "owner/repo", or "owner/*" for org-wide.
    repo_full_name: Mapped[str] = mapped_column(String(140), nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="grants", foreign_keys=[user_id])


class ReviewRun(Base):
    __tablename__ = "review_runs"
    __table_args__ = (
        Index("ix_review_runs_result_gin", "result", postgresql_using="gin"),
        Index("ix_review_runs_repo_pr", "repo_full_name", "pr_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    repo_full_name: Mapped[str] = mapped_column(String(140), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), nullable=False, default=RunStatus.queued, index=True
    )
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    steps: Mapped[list["AgentStep"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentStep.seq"
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_agent_steps_run_seq"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[StepKind] = mapped_column(_enum(StepKind, "step_kind"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    run: Mapped[ReviewRun] = relationship(back_populates="steps")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_metadata_gin", "metadata", postgresql_using="gin"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    # Denormalised so the log survives user deletion.
    actor_email: Mapped[str | None] = mapped_column(String(320))
    actor_ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_type: Mapped[str | None] = mapped_column(String(50))
    resource_id: Mapped[str | None] = mapped_column(String(200))
    outcome: Mapped[Outcome] = mapped_column(_enum(Outcome, "audit_outcome"), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(), index=True
    )
