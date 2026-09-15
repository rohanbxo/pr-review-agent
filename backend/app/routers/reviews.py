"""Review runs.

POST order of checks (each denial is audited and committed on its own):
  1. permission review:create      (app.deps.require)
  2. a repo grant covers the repo  (403)
  3. GitHub says THIS user can read THIS repo (403) — org grants are coarser than GitHub's
     permissions, so the grant alone is not enough
  4. per-user hourly review quota  (429 + Retry-After) — last, so denials never consume quota
then the run and its `review.create` audit row are committed together, and the agent is
queued as a background task.

Visibility for reads: review:read_all sees everything; otherwise a user sees their own runs
plus runs on repos covered by their grants. Invisible runs are 404, not 403, so run ids do not
leak existence.
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from redis.asyncio import Redis
from sqlalchemy import ColumnElement, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm import configured_model_label
from app.agent.runner import run_review
from app.audit import record_audit
from app.db import get_session
from app.deps import require
from app.github_identity import can_read_repo
from app.models import AgentStep, Outcome, ReviewRun, RunStatus, User
from app.ratelimit import enforce_review_quota
from app.rbac import Permission, has_permission
from app.redis_client import get_redis
from app.request_context import RequestContext, get_request_context
from app.validation import grant_covers, validate_repo_full_name

router = APIRouter(prefix="/reviews", tags=["reviews"])


class ReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str = Field(max_length=140)
    pr_number: int = Field(gt=0, le=2_147_483_647)

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        return validate_repo_full_name(v, allow_wildcard=False)


class ReviewRunOut(BaseModel):
    id: uuid.UUID
    repo: str
    pr_number: int
    status: RunStatus
    model: str
    langfuse_trace_id: str | None
    result: dict[str, Any] | None
    usage: dict[str, Any] | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    user_id: int | None

    @classmethod
    def of(cls, run: ReviewRun) -> "ReviewRunOut":
        return cls(
            id=run.id, repo=run.repo_full_name, pr_number=run.pr_number, status=run.status,
            model=run.model, langfuse_trace_id=run.langfuse_trace_id, result=run.result,
            usage=run.usage, error=run.error, created_at=run.created_at,
            started_at=run.started_at, finished_at=run.finished_at, user_id=run.user_id,
        )


class AgentStepOut(BaseModel):
    seq: int
    kind: str
    name: str
    input: dict[str, Any] | list[Any] | None
    output: dict[str, Any] | list[Any] | None
    latency_ms: int | None
    created_at: datetime


def _visibility_clause(user: User) -> ColumnElement[bool] | None:
    """None means unrestricted."""
    if has_permission(user.role, Permission.review_read_all):
        return None
    repo = func.lower(ReviewRun.repo_full_name)
    clauses: list[ColumnElement[bool]] = [ReviewRun.user_id == user.id]
    for grant in user.grants:
        owner, name = grant.repo_full_name.lower().split("/")
        if name == "*":
            clauses.append(repo.startswith(f"{owner}/", autoescape=True))
        else:
            clauses.append(repo == f"{owner}/{name}")
    return or_(*clauses) if clauses else false()


def _can_see(user: User, run: ReviewRun) -> bool:
    if has_permission(user.role, Permission.review_read_all):
        return True
    if run.user_id == user.id:
        return True
    return any(grant_covers(g.repo_full_name, run.repo_full_name) for g in user.grants)


async def _load_visible_run(run_id: uuid.UUID, user: User, session: AsyncSession) -> ReviewRun:
    run = await session.get(ReviewRun, run_id)
    if run is None or not _can_see(user, run):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "review not found")
    return run


async def _deny(
    session: AsyncSession, ctx: RequestContext, user: User, body: ReviewCreate, reason: str
) -> HTTPException:
    record_audit(
        session, ctx=ctx, actor=user, action="review.create", outcome=Outcome.denied,
        resource_type="repo", resource_id=body.repo,
        metadata={"repo": body.repo, "pr_number": body.pr_number, "reason": reason},
    )
    await session.commit()
    return HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=ReviewRunOut)
async def create_review(
    body: ReviewCreate,
    background_tasks: BackgroundTasks,
    user: User = Depends(require(Permission.review_create)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    ctx: RequestContext = Depends(get_request_context),
) -> ReviewRunOut:
    # Holders of grant:manage skip the local grant lookup: they could grant themselves the repo
    # anyway, so requiring it is ceremony. Asked as a Permission, never a role name.
    bypass_grants = has_permission(user.role, Permission.grant_manage)
    if not bypass_grants and not any(grant_covers(g.repo_full_name, body.repo) for g in user.grants):
        raise await _deny(session, ctx, user, body, "no_grant")

    # Non-consuming quota check before spending GitHub API budget on an over-quota user.
    await enforce_review_quota(user, redis, session, ctx, consume=False)

    # Unconditional for EVERY role, admin included: managing the system is not a licence to
    # read repos this person cannot see on GitHub.
    if not await can_read_repo(user_login=user.github_login, repo_full_name=body.repo):
        raise await _deny(session, ctx, user, body, "github_denied")

    await enforce_review_quota(user, redis, session, ctx)

    run = ReviewRun(
        id=uuid.uuid4(), user_id=user.id, repo_full_name=body.repo, pr_number=body.pr_number,
        status=RunStatus.queued, model=configured_model_label(),
    )
    session.add(run)
    record_audit(
        session, ctx=ctx, actor=user, action="review.create", outcome=Outcome.success,
        resource_type="review_run", resource_id=run.id,
        metadata={"repo": body.repo, "pr_number": body.pr_number, "model": run.model},
    )
    await session.commit()  # run + audit row in ONE transaction
    await session.refresh(run)

    background_tasks.add_task(run_review, run.id)
    return ReviewRunOut.of(run)


@router.get("", response_model=list[ReviewRunOut])
async def list_reviews(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(require(Permission.review_read)),
    session: AsyncSession = Depends(get_session),
) -> list[ReviewRunOut]:
    stmt = select(ReviewRun).order_by(ReviewRun.created_at.desc()).limit(limit)
    clause = _visibility_clause(user)
    if clause is not None:
        stmt = stmt.where(clause)
    runs = (await session.scalars(stmt)).all()
    return [ReviewRunOut.of(r) for r in runs]


@router.get("/{run_id}", response_model=ReviewRunOut)
async def get_review(
    run_id: uuid.UUID,
    user: User = Depends(require(Permission.review_read)),
    session: AsyncSession = Depends(get_session),
) -> ReviewRunOut:
    return ReviewRunOut.of(await _load_visible_run(run_id, user, session))


@router.get("/{run_id}/steps", response_model=list[AgentStepOut])
async def get_review_steps(
    run_id: uuid.UUID,
    user: User = Depends(require(Permission.review_read)),
    session: AsyncSession = Depends(get_session),
) -> list[AgentStepOut]:
    run = await _load_visible_run(run_id, user, session)
    steps = (
        await session.scalars(
            select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.seq)
        )
    ).all()
    return [
        AgentStepOut(
            seq=s.seq, kind=s.kind.value, name=s.name, input=s.input, output=s.output,
            latency_ms=s.latency_ms, created_at=s.created_at,
        )
        for s in steps
    ]
